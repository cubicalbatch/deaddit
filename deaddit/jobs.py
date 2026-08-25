"""
Job management system for Deaddit admin UI.
Job execution is owned by the dedicated worker process (deaddit/runtime);
the web process only creates and inspects job rows.
"""

import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from deaddit.config import Config
from deaddit.extensions import db
from deaddit.llm import routing
from deaddit.llm.client import STOP_VALUES, ChatRequest, LLMClient, Sampling
from deaddit.models import Job, JobStatus, JobType
from deaddit.runtime.claim import liveness_is_fresh
from deaddit.services import content

logger = logging.getLogger(__name__)

# Thread-local storage for job progress updates
_thread_local = threading.local()


def _default_app():
    """Lazily resolve the Flask app; callers may pass their own instead."""
    from deaddit import app as _lazy

    return _lazy


def create_job(
    job_type: JobType,
    parameters: dict[str, Any],
    priority: int = 5,
    total_items: int = 1,
    delay_seconds: int = 0,
) -> Job:
    """Create a new pending job row for the worker process to pick up."""

    # Create job record in database
    job = Job(
        type=job_type,
        status=JobStatus.PENDING,
        priority=priority,
        total_items=total_items,
        parameters=parameters,
        rq_job_id=str(uuid.uuid4()),  # Keep field name for compatibility
    )

    db.session.add(job)
    db.session.commit()

    return job


def execute_job(job_id: int, app=None) -> dict[str, Any]:
    """Execute a job based on its type."""

    app = app or _default_app()

    with app.app_context():
        # Store job ID in thread-local storage for progress updates
        _thread_local.job_id = job_id

        # Get the job from database
        job = db.session.get(Job, job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        # Update job status to running
        job.status = JobStatus.RUNNING
        job.started_at = datetime.utcnow()
        db.session.commit()

        # Emit job started update
        _emit_job_update(job)

        try:
            logger.info(f"Executing job {job_id} ({job.type.value})")

            # Execute based on job type
            if job.type == JobType.CREATE_SUBDEADDIT:
                result = _execute_create_subdeaddit(job)
            elif job.type == JobType.CREATE_USER:
                result = _execute_create_user(job)
            elif job.type == JobType.CREATE_POST:
                result = _execute_create_post(job)
            elif job.type == JobType.CREATE_COMMENT:
                result = _execute_create_comment(job)
            elif job.type == JobType.BATCH_OPERATION:
                result = _execute_batch_operation(job)
            else:
                raise ValueError(f"Unknown job type: {job.type}")

            # Update job as completed
            job = db.session.get(Job, job_id)  # Re-fetch to avoid stale data
            job.status = JobStatus.COMPLETED
            job.completed_at = datetime.utcnow()
            job.progress = job.total_items
            job.result = result
            db.session.commit()

            # Emit completion update
            _emit_job_update(job)

            logger.info(f"Job {job_id} completed successfully")
            return result

        except Exception as e:
            # Update job as failed, but try to preserve any partial results (like API requests)
            job = db.session.get(Job, job_id)  # Re-fetch to avoid stale data
            job.status = JobStatus.FAILED
            job.completed_at = datetime.utcnow()
            job.error_message = str(e)

            # Try to get partial results that might contain API requests
            try:
                if job.type == JobType.CREATE_SUBDEADDIT:
                    partial_result = _get_partial_subdeaddit_result()
                elif job.type == JobType.CREATE_USER:
                    partial_result = _get_partial_user_result()
                elif job.type == JobType.CREATE_POST:
                    partial_result = _get_partial_post_result()
                elif job.type == JobType.CREATE_COMMENT:
                    partial_result = _get_partial_comment_result()
                else:
                    partial_result = None

                if partial_result and partial_result.get("api_requests"):
                    job.result = partial_result
                    logger.info(
                        f"Preserved {len(partial_result['api_requests'])} API requests for failed job {job_id}"
                    )
            except Exception as partial_e:
                logger.warning(
                    f"Could not preserve partial results for failed job {job_id}: {partial_e}"
                )

            db.session.commit()

            # Emit failure update
            _emit_job_update(job)

            logger.error(f"Job {job_id} failed: {e}")
            raise


def _get_partial_subdeaddit_result() -> dict[str, Any]:
    """Get partial results for failed subdeaddit creation job."""
    api_requests = getattr(_thread_local, "api_requests", [])
    return {
        "subdeaddits": [],
        "count": 0,
        "api_requests": api_requests,
        "partial": True,
    }


def _get_partial_user_result() -> dict[str, Any]:
    """Get partial results for failed user creation job."""
    api_requests = getattr(_thread_local, "api_requests", [])
    return {"users": [], "count": 0, "api_requests": api_requests, "partial": True}


def _get_partial_post_result() -> dict[str, Any]:
    """Get partial results for failed post creation job."""
    api_requests = getattr(_thread_local, "api_requests", [])
    return {"posts": [], "count": 0, "api_requests": api_requests, "partial": True}


def _get_partial_comment_result() -> dict[str, Any]:
    """Get partial results for failed comment creation job."""
    api_requests = getattr(_thread_local, "api_requests", [])
    return {"comments": [], "count": 0, "api_requests": api_requests, "partial": True}


def _update_job_progress(progress: int):
    """Update job progress in database (thread-safe)."""
    if not hasattr(_thread_local, "job_id"):
        return

    try:
        job = db.session.get(Job, _thread_local.job_id)
        if job:
            job.progress = progress
            db.session.commit()

            # Emit real-time progress update via WebSocket
            _emit_job_update(job)
    except Exception as e:
        logger.warning(f"Could not update job progress: {e}")


def _emit_job_update(job: Job):
    """Emit job update via WebSocket."""
    try:
        from deaddit import socketio

        job_data = job.to_dict()
        socketio.emit(
            "job_update",
            {
                "job_id": job.id,
                "status": job.status.value,
                "progress": job.progress,
                "total_items": job.total_items,
                "error_message": job.error_message,
                "completed_at": job_data["completed_at"],
                "started_at": job_data["started_at"],
            },
            namespace="/admin",
        )

        logger.debug(f"Emitted job update for job {job.id}")
    except Exception as e:
        logger.warning(f"Could not emit job update: {e}")


def _execute_create_subdeaddit(job: Job) -> dict[str, Any]:
    """Execute subdeaddit creation job."""
    params = job.parameters
    count = params.get("count", 1)
    model = params.get("model")
    wait = params.get("wait", 0)

    results = []
    api_requests = []  # Store API requests and responses for debugging

    # Store API requests in thread-local storage for failure recovery
    _thread_local.api_requests = api_requests

    failed_attempts = []

    for i in range(count):
        # Update progress
        _update_job_progress(i)

        retry_count = 0
        max_retries = 3
        success = False

        while retry_count < max_retries and not success:
            try:
                # Generate subdeaddit data using OpenAI API
                subdeaddit_data = _generate_subdeaddit_data(model)

                # Store the API request/response for debugging
                api_requests.append(
                    {
                        "request": subdeaddit_data.get("_api_request"),
                        "response": subdeaddit_data.get("_api_response"),
                        "model_used": subdeaddit_data.get("model"),
                        "retry_attempt": retry_count,
                    }
                )

                # Remove internal fields before ingesting
                clean_subdeaddit_data = {
                    k: v for k, v in subdeaddit_data.items() if not k.startswith("_")
                }

                # Create the subdeaddit via the content service (the legacy
                # ingest endpoint silently upserted, so preserve that).
                content.create_subdeaddit(
                    name=clean_subdeaddit_data["name"],
                    description=clean_subdeaddit_data["description"],
                    post_types=clean_subdeaddit_data.get("post_types") or [],
                    update_if_exists=True,
                )
                subdeaddit_name = clean_subdeaddit_data.get("name", "unknown")
                results.append(subdeaddit_name)
                logger.info(f"Created subdeaddit: {subdeaddit_name}")
                success = True

            except Exception as e:
                retry_count += 1
                error_msg = f"Failed to create subdeaddit {i + 1} (attempt {retry_count}/{max_retries}): {str(e)}"
                logger.warning(error_msg)

                if retry_count >= max_retries:
                    failed_attempts.append(
                        {
                            "subdeaddit_index": i + 1,
                            "error": str(e),
                            "attempts": retry_count,
                        }
                    )
                    logger.error(
                        f"Subdeaddit {i + 1} failed after {max_retries} attempts: {str(e)}"
                    )
                    break
                else:
                    # Wait a bit before retrying
                    time.sleep(2)

        # Wait between creations if specified
        if wait > 0 and i < count - 1:
            time.sleep(wait)

    # If we have some successes but also some failures, log the failures but don't fail the entire job
    if results and failed_attempts:
        logger.warning(
            f"Subdeaddit job completed with {len(results)} successes and {len(failed_attempts)} failures"
        )

    # Only fail the entire job if we got zero successes
    if not results and failed_attempts:
        raise Exception(
            f"All {len(failed_attempts)} subdeaddit creation attempts failed"
        )

    return {
        "subdeaddits": results,
        "count": len(results),
        "failed_attempts": failed_attempts,
        "api_requests": api_requests,
    }


def _execute_create_user(job: Job) -> dict[str, Any]:
    """Execute user creation job."""

    params = job.parameters
    count = params.get("count", 1)
    model = params.get("model")
    wait = params.get("wait", 0)

    results = []
    api_requests = []  # Store API requests and responses for debugging

    # Store API requests in thread-local storage for failure recovery
    _thread_local.api_requests = api_requests

    failed_attempts = []

    for i in range(count):
        # Update progress
        _update_job_progress(i)

        retry_count = 0
        max_retries = 3
        success = False

        while retry_count < max_retries and not success:
            try:
                # Generate user data using OpenAI API
                user_data = _generate_user_data(model)

                # Store the API request/response for debugging
                api_requests.append(
                    {
                        "request": user_data.get("_api_request"),
                        "response": user_data.get("_api_response"),
                        "model_used": user_data.get("model"),
                        "retry_attempt": retry_count,
                    }
                )

                # Remove internal fields before ingesting
                clean_user_data = {
                    k: v for k, v in user_data.items() if not k.startswith("_")
                }

                # Create the user via the content service (interests /
                # personality_traits may arrive as lists — handled there).
                content.create_user(**clean_user_data)
                username = clean_user_data.get("username")
                results.append(username)
                logger.info(f"Created user: {username or 'unknown'}")
                success = True

            except Exception as e:
                retry_count += 1
                error_msg = f"Failed to create user {i + 1} (attempt {retry_count}/{max_retries}): {str(e)}"
                logger.warning(error_msg)

                if retry_count >= max_retries:
                    failed_attempts.append(
                        {"user_index": i + 1, "error": str(e), "attempts": retry_count}
                    )
                    logger.error(
                        f"User {i + 1} failed after {max_retries} attempts: {str(e)}"
                    )
                    break
                else:
                    # Wait a bit before retrying
                    time.sleep(2)

        # Wait between creations if specified
        if wait > 0 and i < count - 1:
            time.sleep(wait)

    # If we have some successes but also some failures, log the failures but don't fail the entire job
    if results and failed_attempts:
        logger.warning(
            f"User job completed with {len(results)} successes and {len(failed_attempts)} failures"
        )

    # Only fail the entire job if we got zero successes
    if not results and failed_attempts:
        raise Exception(f"All {len(failed_attempts)} user creation attempts failed")

    return {
        "users": results,
        "count": len(results),
        "failed_attempts": failed_attempts,
        "api_requests": api_requests,
    }


def _generate_user_data(model: str = None) -> dict[str, Any]:
    """Generate user data using OpenAI API."""
    import json
    import random

    from deaddit.models import User

    # Get existing users for reference
    existing_users = User.query.limit(5).all()
    existing_user_info = []
    for user in existing_users:
        existing_user_info.append(
            {
                "username": user.username,
                "age": user.age,
                "bio": user.bio,
                "writing_style": user.writing_style,
                "interests": user.get_interests()
                if hasattr(user, "get_interests")
                else [],
                "occupation": user.occupation,
                "education": user.education,
                "personality_traits": user.get_personality_traits()
                if hasattr(user, "get_personality_traits")
                else [],
            }
        )
    existing_user_info_json = json.dumps(existing_user_info)

    selected_gender = random.choice(["Male", "Female"])
    education_distribution = [
        ("High school", 0.25),
        ("Some college", 0.25),
        ("Bachelor's degree", 0.35),
        ("Master's degree", 0.1),
        ("PhD", 0.05),
    ]
    selected_education = random.choices(
        [ed for ed, _ in education_distribution],
        weights=[w for _, w in education_distribution],
    )[0]

    system_prompt = """You are an AI assistant tasked with creating realistic user personas for a platform similar to Reddit. Your goal is to create diverse users that represent a wide range of backgrounds, education levels, and interests."""

    prompt = f"""Generate a user persona for a Reddit-like platform. The following attributes are already defined:
    - gender: {selected_gender}
    - education: {selected_education}

    Define the following attributes:
    - username: A unique username. Examples: coolcat92, pizza_lover, life4life, meme_queen. Do not mention books.
    - age: An integer between 18 and 65. Majority should be 18-50, with some older users. Take into account education (no 18 year old with phd)
    - bio: A brief description of the user (1-2 sentences). This should be from an external point of view, not the user's own description.
    - interests: A list of 2-4 interests. Include both niche and popular interests.
    - occupation: Their job or primary activity. Include a mix of blue-collar, white-collar, students, and unemployed.
    - writing_style: A brief description of how they write (1 sentence). Vary between formal, casual, and internet slang.
    - personality_traits: A list of 2-3 personality traits. Include both positive and negative traits.

    Here are some existing users for reference:
    ```
    {existing_user_info_json}
    ```

    Create a user that is different from the existing users and feels like an average internet user. Consider the following guidelines:
    1. Not everyone is an expert or highly educated. Most users should have average knowledge in their interests.
    2. Include users with varying levels of writing skills, from poor grammar to eloquent.
    3. Interests should range from mainstream (sports, movies, gaming) to niche hobbies.
    4. Personality traits should include flaws and quirks, not just positive attributes.
    5. Some users might be lurkers or casual posters rather than highly engaged.

    Provide your response as a JSON object. Make the user feel like a real, relatable individual you might encounter online."""

    # Make OpenAI API request
    api_response, used_model = _send_openai_request(system_prompt, prompt, model)
    user_data = _parse_json_response(api_response, "user")

    if user_data:
        user_data["gender"] = selected_gender
        user_data["education"] = selected_education
        user_data["model"] = used_model

        # Store API request/response for debugging
        user_data["_api_request"] = {
            "system_prompt": system_prompt,
            "prompt": prompt,
            "model": used_model,
        }
        user_data["_api_response"] = api_response
    return user_data or {}


def _send_openai_request(
    system_prompt: str, prompt: str, model: str = None, action: str | None = "job"
) -> tuple[str, str]:
    """Send request to OpenAI API."""
    import random

    OPENAI_API_URL = Config.get("OPENAI_API_URL", "http://localhost/v1")
    OPENAI_KEY = Config.get("OPENAI_KEY", "your_openrouter_api_key")

    # Explicit model wins; otherwise resolve through the routing chain.
    if model:
        selected_api_url, selected_model = OPENAI_API_URL, model
    else:
        selected_api_url, selected_model = routing.resolve()

    temperature = round(random.uniform(0.9, 1), 2)
    logger.info(
        f"Sending request to {selected_api_url} using model {selected_model}, temperature {temperature}"
    )

    request = ChatRequest(
        system_prompt=system_prompt,
        user_prompt=prompt,
        model=selected_model,
        api_url=selected_api_url,
        api_key=OPENAI_KEY,
        sampling=Sampling(temperature=temperature, max_tokens=2048, stop=STOP_VALUES),
    )
    request.action = action
    result = LLMClient().complete(request)
    return result.content, result.model


def _parse_json_response(response: str, content_type: str) -> dict[str, Any]:
    """Parse JSON response from OpenAI API."""
    import json
    import re

    # Clean up the response
    response = response.strip()

    # Remove <think> and </think> tags from AI responses (case-insensitive)
    response = re.sub(
        r"<think>.*?</think>", "", response, flags=re.DOTALL | re.IGNORECASE
    )
    response = response.strip()

    # Try to extract JSON from markdown code blocks first
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
    if json_match:
        response = json_match.group(1)
    else:
        # Find the first complete JSON object in the response
        start_idx = response.find("{")
        if start_idx != -1:
            brace_count = 0
            end_idx = start_idx

            for i in range(start_idx, len(response)):
                if response[i] == "{":
                    brace_count += 1
                elif response[i] == "}":
                    brace_count -= 1
                    if brace_count == 0:
                        end_idx = i + 1
                        break

            if brace_count == 0:
                response = response[start_idx:end_idx]
            else:
                # If braces don't match, try to add missing closing braces
                response = response[start_idx:]
                missing_braces = brace_count
                if missing_braces > 0:
                    response += "}" * missing_braces
                    logger.warning(
                        f"Added {missing_braces} missing closing brace(s) to JSON"
                    )

    # Try to fix trailing commas and other issues
    response = re.sub(r",\s*}", "}", response)  # Remove trailing commas before }
    response = re.sub(r",\s*]", "]", response)  # Remove trailing commas before ]

    try:
        return json.loads(response)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON response for {content_type}: {e}")
        logger.debug(f"Raw response: {response}")

        # Try to extract at least the name and description with regex
        try:
            name_match = re.search(r'"name":\s*"([^"]+)"', response)
            desc_match = re.search(r'"description":\s*"([^"]+(?:\\.[^"]*)*)"', response)
            post_types_match = re.search(r'"post_types":\s*\[([^\]]+)\]', response)

            if name_match and desc_match:
                result = {
                    "name": name_match.group(1),
                    "description": desc_match.group(1)
                    .replace('\\"', '"')
                    .replace("\\n", "\n"),
                }

                if post_types_match:
                    # Parse post types array
                    post_types_str = post_types_match.group(1)
                    post_types = [
                        pt.strip().strip('"') for pt in post_types_str.split(",")
                    ]
                    result["post_types"] = post_types

                logger.info(f"Extracted {content_type} data using regex fallback")
                return result
        except Exception as regex_error:
            logger.error(f"Regex fallback also failed: {regex_error}")

        return {}


def _generate_subdeaddit_data(model: str = None) -> dict[str, Any]:
    """Generate subdeaddit data using OpenAI API."""

    system_prompt = """You are a creative online community manager who is great at coming up with ideas for new online communities and describing what kind of content and discussions would be found in them."""

    prompt = """Please generate a new subreddit, which is an online community focused on a specific topic, interest, or theme. The subreddit should not be image or video based. Provide the following information about the new subreddit:

    - Name: A short, catchy name for the subreddit (no more than 20 characters, preferably one or two words, no spaces)
    - Description: A 2-paragraph description of the subreddit.
        - The first paragraph should clearly explain the main topic, theme, or purpose of the subreddit. What is it about?
        - The second paragraph should highlight what kind of posts and discussions would users find here? Be specific and give examples.
    - post_types: A list of post types that would be common in this subreddit. Choose from the following options (do not invent new types!):
        questions,discussion,polls,opinions,personal,how-to,meta,humor,recommendations,rants,requests,comparisons,challenges,debates,memes,news,reviews,explanations,theories,advice,support,updates,confession,series,creative writing,

    Provide your response in the following JSON format:

    ```json
    {
    "name": "<name>",
    "description": "<paragraph1>. <paragraph2>"
    "post_types": ["type1", "type2", ...]
    }
    ```

    ONLY INCLUDE THE SINGLE JSON OBJECT IN YOUR RESPONSE. DO NOT ADD COMMENT IN THE JSON. MAKE YOUR RESPONSE VALID JSON
    Please generate the new subreddit now."""

    # Make OpenAI API request
    api_response, used_model = _send_openai_request(system_prompt, prompt, model)
    subdeaddit_data = _parse_json_response(api_response, "subdeaddit")

    if subdeaddit_data:
        subdeaddit_data["model"] = used_model

        # Store API request/response for debugging
        subdeaddit_data["_api_request"] = {
            "system_prompt": system_prompt,
            "prompt": prompt,
            "model": used_model,
        }
        subdeaddit_data["_api_response"] = api_response

    return subdeaddit_data or {}


def _generate_post_data(
    subdeaddit_name: str = None, model: str = None
) -> dict[str, Any]:
    """Generate post data using OpenAI API."""
    import random

    from deaddit.models import Subdeaddit, User

    # Get users for weighted selection
    users = User.query.all()  # Get all users instead of limiting to 10
    if not users:
        raise Exception("No users available to create posts")

    # Convert User objects to dictionaries for the weighted selection function
    user_dicts = []
    for user in users:
        user_dicts.append({
            "username": user.username,
            "age": user.age,
            "gender": user.gender,
            "occupation": user.occupation,
            "education": user.education,
            "bio": user.bio,
            "writing_style": user.writing_style,
            "interests": user.get_interests() if hasattr(user, "get_interests") else [],
            "personality_traits": user.get_personality_traits() if hasattr(user, "get_personality_traits") else [],
        })

    # Use weighted selection to favor users with less activity
    # Strategy can be configured via USER_SELECTION_STRATEGY config setting:
    # "weighted" (default) - favors users with fewer posts/comments
    # "round_robin" - always picks user with lowest activity count
    # "improved_random" - better randomness with history avoidance
    from .loader import select_user_smart
    selected_user_dict = select_user_smart(user_dicts, strategy="weighted")

    if not selected_user_dict:
        # Fallback to random selection if weighted selection fails
        logger.warning("Weighted user selection failed, falling back to random selection")
        author = random.choice(users)
    else:
        # Find the actual User object that matches the selected username
        author = next((u for u in users if u.username == selected_user_dict["username"]), None)
        if not author:
            logger.warning(f"Could not find user object for {selected_user_dict['username']}, falling back to random")
            author = random.choice(users)

    # Get subdeaddit info
    if subdeaddit_name:
        subdeaddit = Subdeaddit.query.filter_by(name=subdeaddit_name).first()
        if not subdeaddit:
            raise Exception(f"Subdeaddit '{subdeaddit_name}' not found")
    else:
        # Pick a random subdeaddit
        subdeaddits = Subdeaddit.query.all()
        if not subdeaddits:
            raise Exception("No subdeaddits available to create posts in")
        subdeaddit = random.choice(subdeaddits)

    # Get subdeaddit post types
    post_types = (
        subdeaddit.get_post_types()
        if hasattr(subdeaddit, "get_post_types")
        else ["discussion"]
    )
    selected_post_type = random.choice(post_types) if post_types else "discussion"

    system_prompt = f"""You are {author.username}, a {author.age}-year-old {author.gender.lower()} who works as a {author.occupation}.

Your personality: {author.bio}
Your writing style: {author.writing_style}
Your interests: {", ".join(author.get_interests() if hasattr(author, "get_interests") else [])}

You are creating a post for the /r/{subdeaddit.name} community, which is about: {subdeaddit.description}"""

    prompt = f"""Create a {selected_post_type} post for /r/{subdeaddit.name}.

The post should:
- Be authentic to your personality and interests
- Fit the community theme and typical post type ({selected_post_type})
- Include a compelling title (max 200 characters)
- Have engaging content (2-4 paragraphs)
- Feel natural and realistic
- Use your writing style
- Use \\n for line breaks to separate paragraphs and create proper formatting
- Structure your content with paragraph breaks for better readability

Generate realistic upvote count (typically 5-150 for most posts, rarely higher).

IMPORTANT: Do NOT include user, subdeaddit, or post_type fields in your response - these will be set automatically.

Provide your response as JSON:
```json
{{
    "title": "Your post title",
    "content": "Your first paragraph...\\n\\nSecond paragraph with more details...\\n\\nOptional third paragraph if needed.",
    "upvote_count": 42
}}
```

Create the post now."""

    # Make OpenAI API request
    api_response, used_model = _send_openai_request(system_prompt, prompt, model)
    post_data = _parse_json_response(api_response, "post")

    # Ensure we have a valid dictionary and required fields are always set correctly
    if not post_data:
        post_data = {}

    # Force set required fields to prevent AI from overriding with null/None
    post_data["user"] = author.username
    post_data["subdeaddit"] = subdeaddit.name
    post_data["post_type"] = selected_post_type
    post_data["model"] = used_model

    # Store API request/response for debugging
    post_data["_api_request"] = {
        "system_prompt": system_prompt,
        "prompt": prompt,
        "model": used_model,
    }
    post_data["_api_response"] = api_response

    # Validate that user is not None/null before returning
    if not post_data.get("user"):
        logger.error(
            f"User field is None/empty after assignment. Author: {author.username}, Post data: {post_data}"
        )
        post_data["user"] = author.username  # Force set again

    return post_data


def _generate_comment_data(
    post_id: int = None, subdeaddit_name: str = None, model: str = None
) -> dict[str, Any]:
    """Generate comment data using OpenAI API."""
    import random

    from deaddit.models import Comment, Post, Subdeaddit, User

    # Get users for weighted selection
    users = User.query.all()  # Get all users instead of limiting to 10
    if not users:
        raise Exception("No users available to create comments")

    # Convert User objects to dictionaries for the weighted selection function
    user_dicts = []
    for user in users:
        user_dicts.append({
            "username": user.username,
            "age": user.age,
            "gender": user.gender,
            "occupation": user.occupation,
            "education": user.education,
            "bio": user.bio,
            "writing_style": user.writing_style,
            "interests": user.get_interests() if hasattr(user, "get_interests") else [],
            "personality_traits": user.get_personality_traits() if hasattr(user, "get_personality_traits") else [],
        })

    # Use weighted selection to favor users with less activity
    # Strategy can be configured via USER_SELECTION_STRATEGY config setting:
    # "weighted" (default) - favors users with fewer posts/comments
    # "round_robin" - always picks user with lowest activity count
    # "improved_random" - better randomness with history avoidance
    from .loader import select_user_smart
    selected_user_dict = select_user_smart(user_dicts, strategy="weighted")

    if not selected_user_dict:
        # Fallback to random selection if weighted selection fails
        logger.warning("Weighted user selection failed, falling back to random selection")
        author = random.choice(users)
    else:
        # Find the actual User object that matches the selected username
        author = next((u for u in users if u.username == selected_user_dict["username"]), None)
        if not author:
            logger.warning(f"Could not find user object for {selected_user_dict['username']}, falling back to random")
            author = random.choice(users)

    # Get post to comment on
    if post_id:
        post = Post.query.get(post_id)
        if not post:
            raise Exception(f"Post with ID {post_id} not found")
    else:
        # Pick a random post from the specified subdeaddit or any subdeaddit
        if subdeaddit_name:
            subdeaddit = Subdeaddit.query.filter_by(name=subdeaddit_name).first()
            if not subdeaddit:
                raise Exception(f"Subdeaddit '{subdeaddit_name}' not found")
            posts = Post.query.filter_by(subdeaddit=subdeaddit).all()
        else:
            posts = Post.query.all()

        if not posts:
            raise Exception("No posts available to comment on")
        post = random.choice(posts)

    # Determine if this should be a reply (30% chance, same as CLI loader)
    # Query existing comments directly to ensure proper context
    comments = Comment.query.filter_by(post_id=post.id).all()
    existing_comments = [
        {"id": c.id, "user": c.user, "content": c.content, "parent_id": c.parent_id}
        for c in comments
    ]
    logger.info(
        f"Checking for replies: Found {len(existing_comments)} existing comments for post {post.id}"
    )

    parent_id = None

    if (
        existing_comments and random.random() < 0.3
    ):  # 30% chance to reply to existing comment
        parent_comment = random.choice(existing_comments)
        parent_id = parent_comment.get("id")
        logger.info(
            f"Selected parent comment ID {parent_id} by {parent_comment.get('user', 'unknown')}"
        )
    else:
        logger.info(
            f"Creating top-level comment (no parent selected, {len(existing_comments)} comments available)"
        )

    system_prompt = f"""You are {author.username}, a {author.age}-year-old {author.gender.lower()} who works as a {author.occupation}.

Your personality: {author.bio}
Your writing style: {author.writing_style}
Your interests: {", ".join(author.get_interests() if hasattr(author, "get_interests") else [])}

You are commenting on a post in /r/{post.subdeaddit.name}."""

    # Prepare the prompt based on whether this is a reply or top-level comment
    if parent_id:
        # Find the parent comment data from our existing_comments list
        parent_comment_data = next(
            (c for c in existing_comments if c.get("id") == parent_id), None
        )
        if not parent_comment_data:
            logger.warning(
                f"Could not find parent comment {parent_id}, creating top-level comment"
            )
            parent_id = None

    if parent_id and parent_comment_data:
        # Create reply prompt
        prompt = f"""You're reading this post titled "{post.title}" in /r/{post.subdeaddit.name}:

{post.content}

You're replying to this comment by {parent_comment_data.get("user", "unknown")}:
"{parent_comment_data.get("content", "")}"

Write a reply that:
- Reflects your personality and writing style
- Responds to the specific comment above
- Feels natural and authentic
- Is 1-3 sentences (keep it concise)
- Fits the community tone
- Use \\n for line breaks when you want to separate paragraphs or create emphasis
- Feel free to use multiple paragraphs if it helps express your thoughts clearly

Generate a realistic upvote count for your reply (typically 1-50, sometimes negative).

Provide your response as JSON:
```json
{{
    "content": "Your reply content...\\n\\nSecond paragraph if needed.",
    "upvote_count": 12,
    "user": "{author.username}",
    "post_id": {post.id},
    "parent_id": {parent_id}
}}
```

Write your reply now."""
    else:
        # Create top-level comment prompt
        prompt = f"""You're reading this post titled "{post.title}" in /r/{post.subdeaddit.name}:

{post.content}

Write a comment response that:
- Reflects your personality and writing style
- Is relevant to the post content
- Feels natural and authentic
- Is 1-3 sentences (keep it concise)
- Fits the community tone
- Use \\n for line breaks when you want to separate paragraphs or create emphasis
- Feel free to use multiple paragraphs if it helps express your thoughts clearly

Generate a realistic upvote count for your comment (typically 1-50, sometimes negative).

Provide your response as JSON:
```json
{{
    "content": "Your comment content...\\n\\nSecond paragraph if needed.",
    "upvote_count": 12,
    "user": "{author.username}",
    "post_id": {post.id},
    "parent_id": null
}}
```

Write your comment now."""

    # Make OpenAI API request
    api_response, used_model = _send_openai_request(system_prompt, prompt, model)
    comment_data = _parse_json_response(api_response, "comment")

    if comment_data:
        # Ensure required fields are set correctly
        comment_data["user"] = author.username
        comment_data["post_id"] = post.id
        comment_data["parent_id"] = parent_id
        comment_data["model"] = used_model

        logger.info(
            f"FINAL COMMENT DATA: parent_id={parent_id}, user={author.username}, post_id={post.id}"
        )

        # Store API request/response for debugging
        comment_data["_api_request"] = {
            "system_prompt": system_prompt,
            "prompt": prompt,
            "model": used_model,
        }
        comment_data["_api_response"] = api_response

    return comment_data or {}


def _queue_comment_jobs_for_post(
    post_id: int,
    replies: str,
    model: str = None,
    priority: int = 5,
    wait: int = 0,
):
    """Queue comment generation jobs for a newly created post."""
    import random

    # Parse the replies range (e.g., "5-10", "7", "3-15")
    try:
        if "-" in replies:
            min_replies, max_replies = replies.split("-", 1)
            min_replies = int(min_replies.strip())
            max_replies = int(max_replies.strip())
        else:
            min_replies = max_replies = int(replies.strip())

        # Generate random number of comments within range
        num_comments = random.randint(min_replies, max_replies)

        if not post_id:
            logger.warning("No post ID available; skipping comment job creation")
            return

        # Create a single job to generate all comments for this post
        if num_comments > 0:
            comment_job = create_job(
                job_type=JobType.CREATE_COMMENT,
                parameters={
                    "count": num_comments,
                    "post_id": post_id,
                    "model": model,
                    "wait": wait,
                },
                priority=priority,
                total_items=num_comments,
            )

            logger.info(
                f"Queued comment generation job (ID: {comment_job.id}) for post {post_id}: {num_comments} comments"
            )

    except (ValueError, IndexError) as e:
        logger.warning(f"Could not parse replies range '{replies}': {e}")


def _execute_create_post(job: Job) -> dict[str, Any]:
    """Execute post creation job."""
    params = job.parameters
    count = params.get("count", 1)
    subdeaddit = params.get("subdeaddit")
    replies = params.get("replies", "5-10")
    model = params.get("model")
    wait = params.get("wait", 0)

    results = []
    failed_attempts = []
    api_requests = []  # Store API requests and responses for debugging

    # Store API requests in thread-local storage for failure recovery
    _thread_local.api_requests = api_requests

    for i in range(count):
        # Update progress
        _update_job_progress(i)

        retry_count = 0
        max_retries = 3
        success = False

        while retry_count < max_retries and not success:
            try:
                # Generate post data using OpenAI API
                post_data = _generate_post_data(subdeaddit, model)

                # Store the API request/response for debugging
                api_requests.append(
                    {
                        "request": post_data.get("_api_request"),
                        "response": post_data.get("_api_response"),
                        "model_used": post_data.get("model"),
                        "retry_attempt": retry_count,
                    }
                )

                # Remove internal fields before ingesting
                clean_post_data = {
                    k: v for k, v in post_data.items() if not k.startswith("_")
                }

                # Create the post via the content service
                post = content.create_post(
                    title=clean_post_data["title"],
                    content=clean_post_data["content"],
                    user=clean_post_data["user"],
                    subdeaddit=clean_post_data["subdeaddit"],
                    upvote_count=int(clean_post_data.get("upvote_count") or 0),
                    model=clean_post_data.get("model", "unknown"),
                    post_type=clean_post_data.get("post_type"),
                )

                results.append(int(post.id))
                logger.info(f"Created post {post.id}: {clean_post_data.get('title', 'unknown')}")

                # Queue comment generation jobs if replies are specified
                if replies and replies.strip():
                    _queue_comment_jobs_for_post(
                        int(post.id), replies, model, job.priority, wait
                    )

                success = True

            except Exception as e:
                retry_count += 1
                error_msg = f"Failed to create post {i + 1} (attempt {retry_count}/{max_retries}): {str(e)}"
                logger.warning(error_msg)

                if retry_count >= max_retries:
                    failed_attempts.append(
                        {"post_index": i + 1, "error": str(e), "attempts": retry_count}
                    )
                    logger.error(
                        f"Post {i + 1} failed after {max_retries} attempts: {str(e)}"
                    )
                    break
                else:
                    # Wait a bit before retrying
                    time.sleep(2)

        # Wait between creations if specified
        if wait > 0 and i < count - 1:
            time.sleep(wait)

    # If we have some successes but also some failures, log the failures but don't fail the entire job
    if results and failed_attempts:
        logger.warning(
            f"Job completed with {len(results)} successes and {len(failed_attempts)} failures"
        )

    # Only fail the entire job if we got zero successes
    if not results and failed_attempts:
        raise Exception(f"All {len(failed_attempts)} post creation attempts failed")

    return {
        "posts": results,
        "count": len(results),
        "failed_attempts": failed_attempts,
        "api_requests": api_requests,
    }


def _execute_create_comment(job: Job) -> dict[str, Any]:
    """Execute comment creation job."""
    params = job.parameters
    count = params.get("count", 1)
    post_id = params.get("post_id")
    subdeaddit = params.get("subdeaddit")
    model = params.get("model")
    wait = params.get("wait", 0)

    results = []
    failed_attempts = []
    api_requests = []  # Store API requests and responses for debugging

    # Store API requests in thread-local storage for failure recovery
    _thread_local.api_requests = api_requests

    for i in range(count):
        # Update progress
        _update_job_progress(i)

        retry_count = 0
        max_retries = 3
        success = False

        while retry_count < max_retries and not success:
            try:
                # Generate comment data using OpenAI API
                comment_data = _generate_comment_data(post_id, subdeaddit, model)

                # Store the API request/response for debugging
                api_requests.append(
                    {
                        "request": comment_data.get("_api_request"),
                        "response": comment_data.get("_api_response"),
                        "model_used": comment_data.get("model"),
                        "retry_attempt": retry_count,
                    }
                )

                # Remove internal fields before ingesting
                clean_comment_data = {
                    k: v for k, v in comment_data.items() if not k.startswith("_")
                }

                # Create the comment via the content service
                comment = content.create_comment(
                    post_id=int(clean_comment_data["post_id"]),
                    content=clean_comment_data["content"],
                    user=clean_comment_data["user"],
                    parent_id=clean_comment_data.get("parent_id"),
                    upvote_count=int(clean_comment_data.get("upvote_count") or 0),
                    model=clean_comment_data.get("model", "unknown"),
                )

                results.append(int(comment.id))
                comment_content = clean_comment_data.get("content", "unknown")[:50]
                logger.info(
                    f"Created comment {comment.id} for post {post_id}: {comment_content}"
                )
                success = True

            except Exception as e:
                retry_count += 1
                error_msg = f"Failed to create comment {i + 1} (attempt {retry_count}/{max_retries}): {str(e)}"
                logger.warning(error_msg)

                if retry_count >= max_retries:
                    failed_attempts.append(
                        {
                            "comment_index": i + 1,
                            "error": str(e),
                            "attempts": retry_count,
                        }
                    )
                    logger.error(
                        f"Comment {i + 1} failed after {max_retries} attempts: {str(e)}"
                    )
                    break
                else:
                    # Wait a bit before retrying
                    time.sleep(2)

        # Wait between creations if specified
        if wait > 0 and i < count - 1:
            time.sleep(wait)

    # If we have some successes but also some failures, log the failures but don't fail the entire job
    if results and failed_attempts:
        logger.warning(
            f"Comment job completed with {len(results)} successes and {len(failed_attempts)} failures"
        )

    # Only fail the entire job if we got zero successes
    if not results and failed_attempts:
        raise Exception(f"All {len(failed_attempts)} comment creation attempts failed")

    return {
        "comments": results,
        "count": len(results),
        "failed_attempts": failed_attempts,
        "api_requests": api_requests,
    }


def _execute_batch_operation(job: Job) -> dict[str, Any]:
    """Execute batch operation job."""
    params = job.parameters
    operations = params.get("operations", [])

    results = []

    for i, operation in enumerate(operations):
        # Update progress
        _update_job_progress(i)

        # Create sub-job for each operation
        sub_job = create_job(
            job_type=JobType(operation["type"]),
            parameters=operation["parameters"],
            priority=job.priority,
        )

        results.append({"operation": operation, "job_id": sub_job.id})

    return {"batch_results": results, "count": len(results)}


def get_job_status(job_id: int) -> dict[str, Any] | None:
    """Get the current status of a job."""
    job = db.session.get(Job, job_id)
    if not job:
        return None

    return job.to_dict()


def cancel_job(job_id: int) -> bool:
    """Cancel a pending or running job."""
    job = db.session.get(Job, job_id)
    if not job:
        return False

    if job.status in [JobStatus.PENDING, JobStatus.RUNNING]:
        # Update job status
        job.status = JobStatus.CANCELLED
        job.completed_at = datetime.utcnow()
        db.session.commit()

        logger.info(f"Cancelled job {job_id}")
        return True

    return False


def get_queue_stats() -> dict[str, Any]:
    """Get statistics about the job queue."""
    total_jobs = Job.query.count()
    pending_jobs = Job.query.filter_by(status=JobStatus.PENDING).count()
    running_jobs = Job.query.filter_by(status=JobStatus.RUNNING).count()

    return {
        "scheduler_running": liveness_is_fresh(),
        "total_jobs": total_jobs,
        "pending_jobs": pending_jobs,
        "running_jobs": running_jobs,
        "high_priority": {"pending": 0, "failed": 0},
        "normal": {"pending": pending_jobs, "failed": 0},
        "low_priority": {"pending": 0, "failed": 0},
    }


