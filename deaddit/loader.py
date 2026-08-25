import json
import logging
import random
import re
import time
from collections import defaultdict
from types import SimpleNamespace

import click
from sqlalchemy import func

from .api import build_comment_tree
from .config import Config
from .extensions import db
from .llm import routing
from .llm.client import STOP_VALUES, ChatRequest, LLMClient, Sampling
from .llm.errors import LLMError
from .models import Comment, Post, Subdeaddit, User
from .services import content
from .services.content import ContentValidationError

logger = logging.getLogger(__name__)


# Global cache for post counts per subdeaddit to improve distribution
_subdeaddit_post_counts = defaultdict(int)
_subdeaddit_selection_history = []

# Global cache for user activity to improve distribution
_user_activity_cache = {}
_user_selection_history = []


def get_subdeaddit_post_counts():
    """
    Get current post counts for all subdeaddits from the database.

    Returns:
        dict: Mapping of subdeaddit names to their post counts
    """
    try:
        rows = (
            db.session.query(Post.subdeaddit_name, func.count(Post.id))
            .group_by(Post.subdeaddit_name)
            .all()
        )
        return dict(rows)
    except Exception as e:
        logger.warning(f"Failed to get post counts: {e}")

    return {}


def select_subdeaddit_weighted(subdeaddits):
    """
    Select a subdeaddit using weighted distribution that favors subdeaddits with fewer posts.

    Args:
        subdeaddits (list): List of subdeaddit dictionaries

    Returns:
        dict: Selected subdeaddit
    """
    if not subdeaddits:
        return None

    if len(subdeaddits) == 1:
        return subdeaddits[0]

    # Get current post counts
    post_counts = get_subdeaddit_post_counts()

    # Calculate weights (inverse of post count + 1 to avoid division by zero)
    weights = []
    subdeaddit_names = [sub["name"] for sub in subdeaddits]

    # Find max posts to normalize weights
    max_posts = max([post_counts.get(name, 0) for name in subdeaddit_names]) if post_counts else 0

    for sub_name in subdeaddit_names:
        current_posts = post_counts.get(sub_name, 0)
        # Weight formula: give higher weight to subdeaddits with fewer posts
        # Add 1 to max_posts to ensure weights are never 0
        weight = (max_posts + 1) - current_posts
        weights.append(max(weight, 1))  # Ensure minimum weight of 1

    # Prevent consecutive selections of the same subdeaddit if there are alternatives
    if len(_subdeaddit_selection_history) > 0:
        last_selected = _subdeaddit_selection_history[-1]
        for i, sub in enumerate(subdeaddits):
            if sub["name"] == last_selected and len(subdeaddits) > 1:
                weights[i] = max(1, weights[i] // 3)  # Reduce weight significantly but don't eliminate

    # Use weighted random selection
    selected_subdeaddit = random.choices(subdeaddits, weights=weights, k=1)[0]

    # Update selection history (keep last 5 selections)
    _subdeaddit_selection_history.append(selected_subdeaddit["name"])
    if len(_subdeaddit_selection_history) > 5:
        _subdeaddit_selection_history.pop(0)

    logger.info(f"Weighted selection: {selected_subdeaddit['name']} (current posts: {post_counts.get(selected_subdeaddit['name'], 0)}, weight: {weights[subdeaddits.index(selected_subdeaddit)]})")

    return selected_subdeaddit


def select_subdeaddit_round_robin(subdeaddits):
    """
    Select a subdeaddit using strict round-robin based on current post counts.

    Args:
        subdeaddits (list): List of subdeaddit dictionaries

    Returns:
        dict: Selected subdeaddit with the lowest post count
    """
    if not subdeaddits:
        return None

    if len(subdeaddits) == 1:
        return subdeaddits[0]

    # Get current post counts
    post_counts = get_subdeaddit_post_counts()

    # Find subdeaddit(s) with minimum post count
    min_posts = min([post_counts.get(sub["name"], 0) for sub in subdeaddits])
    candidates = [sub for sub in subdeaddits if post_counts.get(sub["name"], 0) == min_posts]

    # If multiple candidates have the same minimum count, use weighted random among them
    if len(candidates) > 1:
        # Avoid consecutive selections from history
        if len(_subdeaddit_selection_history) > 0:
            last_selected = _subdeaddit_selection_history[-1]
            non_recent = [sub for sub in candidates if sub["name"] != last_selected]
            if non_recent:
                candidates = non_recent

        selected = random.choice(candidates)
    else:
        selected = candidates[0]

    # Update selection history
    _subdeaddit_selection_history.append(selected["name"])
    if len(_subdeaddit_selection_history) > 5:
        _subdeaddit_selection_history.pop(0)

    logger.info(f"Round-robin selection: {selected['name']} (current posts: {post_counts.get(selected['name'], 0)})")

    return selected


def select_subdeaddit_improved_random(subdeaddits):
    """
    Select a subdeaddit using improved random selection with better seeding.

    Args:
        subdeaddits (list): List of subdeaddit dictionaries

    Returns:
        dict: Selected subdeaddit
    """
    if not subdeaddits:
        return None

    if len(subdeaddits) == 1:
        return subdeaddits[0]

    # Use better entropy for random selection
    import secrets

    # Avoid consecutive selections if possible
    available_subs = subdeaddits.copy()
    if len(_subdeaddit_selection_history) > 0 and len(subdeaddits) > 1:
        last_selected = _subdeaddit_selection_history[-1]
        available_subs = [sub for sub in subdeaddits if sub["name"] != last_selected]
        if not available_subs:  # Fallback if all are filtered out
            available_subs = subdeaddits

    # Use secrets module for cryptographically secure randomness
    selected = secrets.choice(available_subs)

    # Update selection history
    _subdeaddit_selection_history.append(selected["name"])
    if len(_subdeaddit_selection_history) > 5:
        _subdeaddit_selection_history.pop(0)

    logger.info(f"Improved random selection: {selected['name']}")

    return selected


def select_subdeaddit_smart(subdeaddits, strategy="weighted"):
    """
    Smart subdeaddit selection with configurable strategy.

    Args:
        subdeaddits (list): List of subdeaddit dictionaries
        strategy (str): Selection strategy - "weighted", "round_robin", or "improved_random"

    Returns:
        dict: Selected subdeaddit
    """
    if not subdeaddits:
        return None

    # Get strategy from config if available
    config_strategy = Config.get("SUBDEADDIT_SELECTION_STRATEGY", strategy)

    if config_strategy == "round_robin":
        return select_subdeaddit_round_robin(subdeaddits)
    elif config_strategy == "improved_random":
        return select_subdeaddit_improved_random(subdeaddits)
    else:  # Default to weighted
        return select_subdeaddit_weighted(subdeaddits)


def get_user_activity_counts():
    """
    Get current post and comment counts for all users from the database.

    Returns:
        dict: Mapping of usernames to their total activity counts (posts + comments)
    """
    try:
        activity_counts = defaultdict(int)

        # Count posts per user
        post_rows = (
            db.session.query(Post.user, func.count(Post.id)).group_by(Post.user).all()
        )
        for user, count in post_rows:
            activity_counts[user] += count

        # Count comments per user
        comment_rows = (
            db.session.query(Comment.user, func.count(Comment.id))
            .group_by(Comment.user)
            .all()
        )
        for user, count in comment_rows:
            activity_counts[user] += count

        return dict(activity_counts)

    except Exception as e:
        logger.warning(f"Failed to get user activity counts: {e}")

    return {}


def _serialize_user(user: User) -> dict:
    """
    Convert a User ORM object to the same dict shape the /api/users JSON
    returned (interests / personality_traits as parsed lists) so frozen
    selection heuristics keep working unchanged.
    """
    return {
        "username": user.username,
        "age": user.age,
        "gender": user.gender,
        "bio": user.bio,
        "interests": json.loads(user.interests) if user.interests else [],
        "occupation": user.occupation,
        "education": user.education,
        "writing_style": user.writing_style,
        "personality_traits": (
            json.loads(user.personality_traits) if user.personality_traits else []
        ),
        "model": user.model
        if isinstance(user.model, str)
        else json.loads(user.model)
        if user.model
        else "unknown",
    }


def select_user_weighted(users):
    """
    Select a user using weighted distribution that favors users with fewer posts/comments.

    Args:
        users (list): List of user dictionaries

    Returns:
        dict: Selected user
    """
    if not users:
        return None

    if len(users) == 1:
        return users[0]

    # Get current activity counts
    activity_counts = get_user_activity_counts()

    # Calculate weights (inverse of activity count + 1 to avoid division by zero)
    weights = []
    usernames = [user["username"] for user in users]

    # Find max activity to normalize weights
    max_activity = max([activity_counts.get(username, 0) for username in usernames]) if activity_counts else 0

    for username in usernames:
        current_activity = activity_counts.get(username, 0)
        # Weight formula: give higher weight to users with fewer posts/comments
        # Add 1 to max_activity to ensure weights are never 0
        weight = (max_activity + 1) - current_activity
        weights.append(max(weight, 1))  # Ensure minimum weight of 1

    # Prevent consecutive selections of the same user if there are alternatives
    if len(_user_selection_history) > 0:
        last_selected = _user_selection_history[-1]
        for i, user in enumerate(users):
            if user["username"] == last_selected and len(users) > 1:
                weights[i] = max(1, weights[i] // 3)  # Reduce weight significantly but don't eliminate

    # Use weighted random selection
    selected_user = random.choices(users, weights=weights, k=1)[0]

    # Update selection history (keep last 5 selections)
    _user_selection_history.append(selected_user["username"])
    if len(_user_selection_history) > 5:
        _user_selection_history.pop(0)

    logger.info(f"Weighted user selection: {selected_user['username']} (current activity: {activity_counts.get(selected_user['username'], 0)}, weight: {weights[users.index(selected_user)]})")

    return selected_user


def select_user_round_robin(users):
    """
    Select a user using strict round-robin based on current activity counts.

    Args:
        users (list): List of user dictionaries

    Returns:
        dict: Selected user with the lowest activity count
    """
    if not users:
        return None

    if len(users) == 1:
        return users[0]

    # Get current activity counts
    activity_counts = get_user_activity_counts()

    # Find user(s) with minimum activity count
    min_activity = min([activity_counts.get(user["username"], 0) for user in users])
    candidates = [user for user in users if activity_counts.get(user["username"], 0) == min_activity]

    # If multiple candidates have the same minimum count, use weighted random among them
    if len(candidates) > 1:
        # Avoid consecutive selections from history
        if len(_user_selection_history) > 0:
            last_selected = _user_selection_history[-1]
            non_recent = [user for user in candidates if user["username"] != last_selected]
            if non_recent:
                candidates = non_recent

        selected = random.choice(candidates)
    else:
        selected = candidates[0]

    # Update selection history
    _user_selection_history.append(selected["username"])
    if len(_user_selection_history) > 5:
        _user_selection_history.pop(0)

    logger.info(f"Round-robin user selection: {selected['username']} (current activity: {activity_counts.get(selected['username'], 0)})")

    return selected


def select_user_improved_random(users):
    """
    Select a user using improved random selection with better seeding.

    Args:
        users (list): List of user dictionaries

    Returns:
        dict: Selected user
    """
    if not users:
        return None

    if len(users) == 1:
        return users[0]

    # Use better entropy for random selection
    import secrets

    # Avoid consecutive selections if possible
    available_users = users.copy()
    if len(_user_selection_history) > 0 and len(users) > 1:
        last_selected = _user_selection_history[-1]
        available_users = [user for user in users if user["username"] != last_selected]
        if not available_users:  # Fallback if all are filtered out
            available_users = users

    # Use secrets module for cryptographically secure randomness
    selected = secrets.choice(available_users)

    # Update selection history
    _user_selection_history.append(selected["username"])
    if len(_user_selection_history) > 5:
        _user_selection_history.pop(0)

    logger.info(f"Improved random user selection: {selected['username']}")

    return selected


def select_user_smart(users, strategy="weighted"):
    """
    Smart user selection with configurable strategy.

    Args:
        users (list): List of user dictionaries
        strategy (str): Selection strategy - "weighted", "round_robin", or "improved_random"

    Returns:
        dict: Selected user
    """
    if not users:
        return None

    # Get strategy from config if available
    config_strategy = Config.get("USER_SELECTION_STRATEGY", strategy)

    if config_strategy == "round_robin":
        return select_user_round_robin(users)
    elif config_strategy == "improved_random":
        return select_user_improved_random(users)
    else:  # Default to weighted
        return select_user_weighted(users)


def test_subdeaddit_distribution(num_tests=100, strategy="weighted"):
    """
    Test subdeaddit selection distribution to verify randomization improvements.

    Args:
        num_tests (int): Number of selections to test
        strategy (str): Selection strategy to test

    Returns:
        dict: Distribution results and statistics
    """
    logger.info(f"Testing subdeaddit distribution with {num_tests} selections using {strategy} strategy")

    # Get subdeaddits from the database (same dict shape as /api/subdeaddits)
    try:
        subs = [
            {
                "name": s.name,
                "description": s.description,
                "post_types": s.get_post_types(),
            }
            for s in Subdeaddit.query.all()
        ]
        if not subs:
            logger.error("No subdeaddits found for testing")
            return None

        logger.info(f"Testing with {len(subs)} subdeaddits")

        # Reset selection history for clean test
        global _subdeaddit_selection_history
        _subdeaddit_selection_history.clear()

        # Run selections
        selection_counts = defaultdict(int)
        selections = []

        for _ in range(num_tests):
            if strategy == "round_robin":
                selected = select_subdeaddit_round_robin(subs)
            elif strategy == "improved_random":
                selected = select_subdeaddit_improved_random(subs)
            else:  # weighted
                selected = select_subdeaddit_weighted(subs)

            if selected:
                sub_name = selected["name"]
                selection_counts[sub_name] += 1
                selections.append(sub_name)

        # Calculate statistics
        total_selections = sum(selection_counts.values())
        expected_per_sub = total_selections / len(subs)

        results = {
            "strategy": strategy,
            "total_selections": total_selections,
            "num_subdeaddits": len(subs),
            "expected_per_sub": expected_per_sub,
            "selection_counts": dict(selection_counts),
            "selections": selections,
        }

        # Calculate variance and standard deviation
        variances = []
        for sub in subs:
            actual_count = selection_counts[sub["name"]]
            variance = (actual_count - expected_per_sub) ** 2
            variances.append(variance)

        results["variance"] = sum(variances) / len(variances)
        results["std_deviation"] = results["variance"] ** 0.5

        # Calculate distribution quality metrics
        min_count = min(selection_counts.values()) if selection_counts else 0
        max_count = max(selection_counts.values()) if selection_counts else 0
        results["min_selections"] = min_count
        results["max_selections"] = max_count
        results["range"] = max_count - min_count
        results["range_ratio"] = max_count / min_count if min_count > 0 else float('inf')

        # Count subdeaddits with zero selections
        results["zero_selections"] = len(subs) - len(selection_counts)

        logger.info(f"Distribution test results for {strategy}:")
        logger.info(f"  - Range: {min_count} to {max_count} (ratio: {results['range_ratio']:.2f})")
        logger.info(f"  - Standard deviation: {results['std_deviation']:.2f}")
        logger.info(f"  - Subdeaddits with zero selections: {results['zero_selections']}")

        return results

    except Exception as e:
        logger.error(f"Error testing subdeaddit distribution: {e}")
        return None


def test_user_distribution(num_tests=100, strategy="weighted"):
    """
    Test user selection distribution to verify randomization improvements.

    Args:
        num_tests (int): Number of selections to test
        strategy (str): Selection strategy to test

    Returns:
        dict: Distribution results and statistics
    """
    logger.info(f"Testing user distribution with {num_tests} selections using {strategy} strategy")

    # Get users from the database (same dict shape as /api/users)
    try:
        users = [_serialize_user(u) for u in User.query.all()]
        if not users:
            logger.error("No users found for testing")
            return None

        logger.info(f"Testing with {len(users)} users")

        # Reset selection history for clean test
        global _user_selection_history
        _user_selection_history.clear()

        # Run selections
        selection_counts = defaultdict(int)
        selections = []

        for _ in range(num_tests):
            if strategy == "round_robin":
                selected = select_user_round_robin(users)
            elif strategy == "improved_random":
                selected = select_user_improved_random(users)
            elif strategy == "random":
                selected = random.choice(users)
            else:  # weighted
                selected = select_user_weighted(users)

            if selected:
                username = selected["username"]
                selection_counts[username] += 1
                selections.append(username)

        # Calculate statistics
        total_selections = sum(selection_counts.values())
        expected_per_user = total_selections / len(users)

        results = {
            "strategy": strategy,
            "total_selections": total_selections,
            "num_users": len(users),
            "expected_per_user": expected_per_user,
            "selection_counts": dict(selection_counts),
            "selections": selections,
        }

        # Calculate variance and standard deviation
        variances = []
        for user in users:
            actual_count = selection_counts[user["username"]]
            variance = (actual_count - expected_per_user) ** 2
            variances.append(variance)

        results["variance"] = sum(variances) / len(variances)
        results["std_deviation"] = results["variance"] ** 0.5

        # Calculate distribution quality metrics
        min_count = min(selection_counts.values()) if selection_counts else 0
        max_count = max(selection_counts.values()) if selection_counts else 0
        results["min_selections"] = min_count
        results["max_selections"] = max_count
        results["range"] = max_count - min_count
        results["range_ratio"] = max_count / min_count if min_count > 0 else float('inf')

        # Count users with zero selections
        results["zero_selections"] = len(users) - len(selection_counts)

        logger.info(f"Distribution test results for {strategy}:")
        logger.info(f"  - Range: {min_count} to {max_count} (ratio: {results['range_ratio']:.2f})")
        logger.info(f"  - Standard deviation: {results['std_deviation']:.2f}")
        logger.info(f"  - Users with zero selections: {results['zero_selections']}")

        return results

    except Exception as e:
        logger.error(f"Error testing user distribution: {e}")
        return None


def get_dynamic_temperature(user_personality_traits, content_type="post"):
    """
    Calculate dynamic temperature based on user personality and content type.

    Args:
        user_personality_traits (list): List of personality traits
        content_type (str): Type of content being generated

    Returns:
        float: Temperature value between 0.3 and 1.3
    """
    base_temp = 0.8

    # Personality-based adjustments
    traits_str = " ".join(user_personality_traits).lower()

    # Creative personalities get higher temperature
    if any(
        trait in traits_str
        for trait in ["creative", "artistic", "imaginative", "spontaneous", "quirky"]
    ):
        base_temp += 0.3

    # Analytical personalities get lower temperature
    if any(
        trait in traits_str
        for trait in ["analytical", "logical", "methodical", "precise", "systematic"]
    ):
        base_temp -= 0.2

    # Emotional personalities get moderate increase
    if any(
        trait in traits_str
        for trait in ["emotional", "empathetic", "passionate", "expressive"]
    ):
        base_temp += 0.15

    # Cautious personalities get lower temperature
    if any(
        trait in traits_str
        for trait in ["cautious", "reserved", "conservative", "careful"]
    ):
        base_temp -= 0.15

    # Content type adjustments
    if content_type == "comment":
        base_temp += 0.1  # Comments can be more spontaneous
    elif content_type == "reply":
        base_temp += 0.15  # Replies can be even more reactive

    # Add small random variation
    base_temp += random.uniform(-0.1, 0.1)

    # Clamp to valid range
    return max(0.3, min(1.3, round(base_temp, 2)))


def send_request(
    system_prompt: str, prompt: str, user_personality_traits=None, content_type="post"
) -> dict:
    """
    Send a request to the AI API with enhanced error handling and fallback mechanisms.

    Args:
        system_prompt (str): The system prompt for the AI server.
        prompt (str): The user prompt for the AI server.
        user_personality_traits (list): User personality traits
        content_type (str): Type of content being generated

    Returns:
        tuple: (response_object, model_name)
    """
    OPENAI_API_URL = Config.get("OPENAI_API_URL", "http://localhost/v1")
    OPENAI_KEY = Config.get("OPENAI_KEY", "your_openrouter_api_key")

    # Determine user persona for model selection
    user_persona = None
    if user_personality_traits:
        user_persona = " ".join(user_personality_traits)

    selected_api_url, selected_model = routing.resolve(user_persona)

    # Add /nothink prefix for specific models
    if any(keyword in selected_model.lower() for keyword in ["qwen", "qwq", "deepseek"]):
        prompt = "/nothink " + prompt

    # Use dynamic temperature based on personality
    if user_personality_traits:
        temperature = get_dynamic_temperature(user_personality_traits, content_type)
    else:
        temperature = round(random.uniform(0.7, 1.1), 2)
    logger.info(
        f"Sending prompt to the server {selected_api_url} using model {selected_model}. Temperature chosen: {temperature}. Prompt length: {len(prompt)} characters."
    )
    # Dynamic max_tokens based on personality and content type
    max_tokens = 1300  # default
    if user_personality_traits:
        traits_str = " ".join(user_personality_traits).lower()

        # Verbose personalities get more tokens
        if any(
            trait in traits_str
            for trait in ["verbose", "detailed", "analytical", "thorough"]
        ):
            max_tokens = 1600
        # Concise personalities get fewer tokens
        elif any(
            trait in traits_str for trait in ["concise", "brief", "laconic", "reserved"]
        ):
            max_tokens = 800
        # Creative personalities get variable tokens
        elif any(
            trait in traits_str for trait in ["creative", "expressive", "dramatic"]
        ):
            max_tokens = random.randint(1000, 1500)

    # Content type adjustments
    if content_type == "comment":
        max_tokens = int(max_tokens * 0.7)  # Comments are typically shorter
    elif content_type == "reply":
        max_tokens = int(max_tokens * 0.5)  # Replies are usually brief

    request = ChatRequest(
        system_prompt=system_prompt,
        user_prompt=prompt,
        model=selected_model,
        api_url=selected_api_url,
        api_key=OPENAI_KEY,
        sampling=Sampling(temperature=temperature, max_tokens=max_tokens, stop=STOP_VALUES),
    )
    request.action = content_type
    if "openrouter" in OPENAI_API_URL:
        request.extra_payload = {"provider": {"allow_fallbacks": False}}

    try:
        result = LLMClient().complete(request)
    except LLMError as e:
        logger.error(f"LLM call failed: {e}")
        return None

    reconstructed_response = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=result.content))
        ],
        usage=SimpleNamespace(**result.usage),
    )
    logger.info(f"Response received using model {result.model}.")
    return reconstructed_response, result.model


def parse_data(api_response: dict, type: str, subdeaddit_name: str = "") -> dict:
    if api_response is None:
        logger.error("API response is None, cannot parse data")
        return {}

    try:
        generated_text = api_response.choices[0].message.content.strip()
    except (AttributeError, IndexError) as e:
        logger.error(f"Error accessing API response content: {str(e)}")
        return {}

    # Remove <think> and </think> tags from AI responses (case-insensitive)
    generated_text = re.sub(
        r"<think>.*?</think>", "", generated_text, flags=re.DOTALL | re.IGNORECASE
    )
    generated_text = generated_text.strip()

    logger.info(f"Received text: {generated_text}")

    # Try to extract JSON from the text
    json_str = ""
    brace_count = 0
    in_json = False
    for line in generated_text.split("\n"):
        if "{" in line and not in_json:
            in_json = True
        if in_json:
            json_str += line + "\n"
            brace_count += line.count("{") - line.count("}")
        if brace_count == 0 and in_json:
            break

    # Ensure JSON is properly closed
    if brace_count > 0:
        json_str += "}" * brace_count
        logger.info(f"Added {brace_count} closing braces to complete JSON structure")

    if not json_str:
        logger.error("No JSON object found in the response")
        return {}

    # Function to fix common JSON issues
    def fix_json(json_str):
        # Replace single quotes with double quotes
        json_str = json_str.replace("'", '"')
        # Remove trailing commas
        json_str = re.sub(r",\s*}", "}", json_str)
        json_str = re.sub(r",\s*]", "]", json_str)
        # Add missing commas between key-value pairs
        json_str = re.sub(r'"\s*}\s*"', '",\n"', json_str)
        # Ensure all keys are in double quotes
        json_str = re.sub(r"(\w+)(?=\s*:)", r'"\1"', json_str)
        # Remove any non-JSON content before the first '{' and after the last '}'
        json_str = re.sub(r"^[^{]*", "", json_str)
        json_str = re.sub(r"[^}]*$", "", json_str)

        # Replace <p> with <br> for line breaks
        json_str = json_str.replace("<p>", "<br>")
        # Remove </p>
        json_str = json_str.replace("</p>", "")

        # Replace \n with <br> for line breaks
        json_str = json_str.replace("\\n", "<br>")
        return json_str.strip()

    # Try to parse the JSON
    try:
        json_data = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON: {e}")
        # Attempt to fix common JSON issues
        fixed_json_str = fix_json(json_str)
        try:
            json_data = json.loads(fixed_json_str)
            logger.info("Successfully parsed JSON after fixes")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON even after attempted fixes: {e}")
            logger.error(f"Problematic JSON string: {fixed_json_str}")
            return {}

    # Function to convert keys to lowercase recursively
    def lowercase_keys(obj):
        if isinstance(obj, dict):
            return {k.lower(): lowercase_keys(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [lowercase_keys(elem) for elem in obj]
        else:
            return obj

    # Convert all keys to lowercase
    json_data = lowercase_keys(json_data)

    # Extract the relevant data based on the type
    if type == "post":
        if (
            "posts" in json_data
            and isinstance(json_data["posts"], list)
            and len(json_data["posts"]) > 0
        ):
            data = json_data["posts"][0]
        else:
            data = json_data
    elif type in ["subdeaddit", "comment", "user"]:
        data = json_data
    else:
        logger.error(f"Unknown data type: {type}")
        return {}

    # Add subdeaddit name if provided
    if subdeaddit_name:
        data["subdeaddit"] = subdeaddit_name

    # Ensure all values are strings
    try:
        for key, value in data.items():
            if isinstance(value, (int | float)):
                data[key] = str(value)
    except AttributeError:
        logger.error("Failed to convert values to strings")
        return {}

    logger.info(f"Parsed data: {data}")
    return data

def get_random_user(strategy="weighted"):
    """
    Get a user using smart selection that favors users with less activity.

    Args:
        strategy (str): Selection strategy - "weighted", "round_robin", "improved_random", or "random"

    Returns:
        dict: Selected user or None if error
    """
    try:
        users = [_serialize_user(u) for u in User.query.all()]
        if not users:
            logger.error("No users found in the database")
            return None

        # Use old random selection if explicitly requested
        if strategy == "random":
            randomly_selected_user = random.choice(users)
            logger.info(f"Randomly selected user: {randomly_selected_user['username']}")
            return randomly_selected_user

        # Use smart weighted selection by default
        selected_user = select_user_smart(users, strategy)
        if selected_user:
            return selected_user
        else:
            # Fallback to simple random if smart selection fails
            logger.warning("Smart user selection failed, falling back to random selection")
            randomly_selected_user = random.choice(users)
            logger.info(f"Fallback randomly selected user: {randomly_selected_user['username']}")
            return randomly_selected_user

    except Exception as e:
        logger.error(f"Unexpected error in get_random_user: {str(e)}")
        return None


def get_post_type_description(post_type: str) -> str:
    post_types = {
        "questions": "Pose an open-ended or thought-provoking question relevant to the subdeaddit's topic.",
        "discussion": "Present a topic or issue to encourage dialogue and exchange of ideas among users.",
        "polls": "Create a multiple-choice question or survey to gather opinions on a relevant topic.",
        "opinions": "Express a well-reasoned viewpoint on a subject related to the subdeaddit's theme.",
        "personal stories": "Share a fictional first-person account or anecdote relevant to the subdeaddit's focus.",
        "how-to": "Provide step-by-step instructions or advice on accomplishing a task related to the subdeaddit's theme.",
        "meta": "Discuss the subdeaddit itself, its rules, or trends within the community.",
        "humor": "Create a joke, pun, or humorous observation relevant to the subdeaddit's topic.",
        "recommendations": "Suggest books, products, or resources related to the subdeaddit's subject matter.",
        "rants": "Express frustration or criticism about a topic relevant to the subdeaddit.",
        "requests": "Ask for specific information, advice, or resources from the community.",
        "comparisons": "Analyze similarities and differences between two or more related topics within the subdeaddit's theme.",
        "challenges": "Propose a task or contest for community members related to the subdeaddit's focus.",
        "debates": "Present two opposing viewpoints on a controversial topic relevant to the subdeaddit.",
        "memes": "Create a humorous text-based post that references common themes in the subdeaddit.",
        "news": "Share and summarize a recent event or development related to the subdeaddit's topic.",
        "reviews": "Provide a critical evaluation of a product, service, or media relevant to the subdeaddit.",
        "explanations": "Offer a clear and detailed explanation of a concept related to the subdeaddit's theme.",
        "theories": "Present a speculative idea or hypothesis about a topic relevant to the subdeaddit.",
        "advice": "Offer guidance or suggestions to help with a problem related to the subdeaddit's focus.",
        "support": "Provide encouragement or empathy for users dealing with challenges related to the subdeaddit's theme.",
        "updates": "Share new information or developments on a previously discussed topic in the subdeaddit.",
        "confession": "Share a fictional admission of wrongdoing or secret relevant to the subdeaddit's theme.",
        "series": "Create an episodic post that continues or references previous entries in a storyline.",
        "creative writing": "Compose an original piece of fiction or poetry related to the subdeaddit's theme.",
    }
    post_type_description = post_types.get(
        post_type, "Create a post relevant to the subdeaddit's theme."
    )
    return post_type_description


def get_personality_archetype(personality_traits):
    """
    Determine the user's personality archetype based on their traits.

    Args:
        personality_traits (list): List of personality traits

    Returns:
        str: Primary personality archetype
    """
    traits_str = " ".join(personality_traits).lower()

    # Define personality archetypes
    if any(
        trait in traits_str
        for trait in ["analytical", "logical", "methodical", "systematic", "precise"]
    ):
        return "analytical"
    elif any(
        trait in traits_str
        for trait in ["creative", "artistic", "imaginative", "expressive", "quirky"]
    ):
        return "creative"
    elif any(
        trait in traits_str
        for trait in ["social", "outgoing", "friendly", "extroverted", "charismatic"]
    ):
        return "social"
    elif any(
        trait in traits_str
        for trait in ["skeptical", "critical", "cynical", "contrarian", "argumentative"]
    ):
        return "contrarian"
    elif any(
        trait in traits_str
        for trait in ["empathetic", "supportive", "caring", "emotional", "sensitive"]
    ):
        return "empathetic"
    elif any(
        trait in traits_str
        for trait in ["humorous", "witty", "sarcastic", "funny", "playful"]
    ):
        return "humorous"
    elif any(
        trait in traits_str
        for trait in ["cautious", "reserved", "conservative", "careful", "introverted"]
    ):
        return "reserved"
    else:
        return "balanced"


def get_system_prompt(user: dict, content_type="post", subdeaddit_context=None) -> str:
    """
    Generate a dynamic system prompt based on user personality and context.

    Args:
        user (dict): User information
        content_type (str): Type of content being generated
        subdeaddit_context (dict, optional): Subdeaddit information for context

    Returns:
        str: Customized system prompt
    """
    personality_archetype = get_personality_archetype(user["personality_traits"])

    # Base prompt components
    base_identity = f"""You are an AI generating authentic content for Deaddit as {user["username"]}, a {user["age"]}-year-old {user["gender"].lower()} {user["occupation"].lower()}."""

    # Personality-specific behavioral guidelines
    personality_guidelines = {
        "analytical": """Your approach is methodical and fact-based. You appreciate data, logical reasoning, and well-structured arguments. You tend to break down complex topics, ask clarifying questions, and provide detailed explanations. Your posts often include statistics, references, or step-by-step analysis.""",
        "creative": """Your mind works in unique ways, often making unexpected connections between ideas. You express yourself through metaphors, analogies, and creative examples. Your content tends to be imaginative, sometimes abstract, and you're not afraid to think outside conventional boundaries.""",
        "social": """You naturally engage with others and create inclusive conversations. Your posts often invite community participation, share relatable experiences, and build connections. You're skilled at reading social dynamics and adapting your communication style to bring people together.""",
        "contrarian": """You naturally question popular opinions and aren't afraid to voice unpopular viewpoints. Your posts often challenge assumptions, point out inconsistencies, or present alternative perspectives. You value intellectual honesty over social harmony.""",
        "empathetic": """You deeply understand others' emotions and experiences. Your content often provides emotional support, validates feelings, and offers compassionate perspectives. You're skilled at reading between the lines of what people are really expressing.""",
        "humorous": """You see the lighter side of situations and use humor to connect with others. Your posts often include witty observations, clever wordplay, or amusing anecdotes. You know how to use humor appropriately without being insensitive.""",
        "reserved": """You prefer thoughtful, measured responses over impulsive reactions. Your posts are typically well-considered and concise. You're more likely to observe conversations before contributing, and when you do, it's usually substantive.""",
        "balanced": """You adapt your communication style to the situation and topic. You can be analytical when needed, creative when inspired, and social when appropriate. Your responses are contextually appropriate and well-rounded.""",
    }

    # Education and occupation influence
    expertise_context = ""
    if user["education"] in ["Bachelor's degree", "Master's degree", "PhD"]:
        expertise_context = " You communicate with the vocabulary and depth expected of someone with higher education, but avoid being condescending."
    elif user["education"] in ["High school", "Some college"]:
        expertise_context = " Your communication style is straightforward and practical, focusing on real-world applications rather than abstract theories."

    # Writing style integration
    style_guidance = f" Your natural writing style is {user['writing_style'].lower()}, which influences your tone, vocabulary choice, and sentence structure."

    # Content type specific instructions
    content_instructions = {
        "post": "You're creating an original post that should spark discussion and engagement within the community.",
        "comment": "You're responding to a post with your genuine reaction, opinion, or additional perspective.",
        "reply": "You're directly engaging with another user's comment, creating a natural conversation flow.",
    }

    # Subdeaddit context if provided
    community_context = ""
    if subdeaddit_context:
        community_context = f" You're familiar with r/{subdeaddit_context['name']} and understand its community culture and typical discussion patterns."

    # Authenticity reminders
    authenticity_rules = """\n\nAuthenticity Guidelines:
- Write as this specific person would, with their unique perspective and voice
- Include natural imperfections, personal biases, and individual quirks
- React genuinely to content based on your interests and personality
- Use vocabulary and references appropriate to your age, background, and interests
- Avoid generic responses that could come from anyone
- Don't explicitly state your background unless naturally relevant
- No greetings like "Hey everyone" or "Fellow redditors" - jump into your point
- Let your personality show through your choice of examples, analogies, and focus areas"""

    return f"""{base_identity}

{personality_guidelines[personality_archetype]}{expertise_context}{style_guidance}

{content_instructions[content_type]}{community_context}

Interests: {", ".join(user["interests"])}
Personality: {", ".join(user["personality_traits"])}{authenticity_rules}"""


def analyze_community_culture(subdeaddit_info, recent_posts):
    """
    Analyze community culture based on subdeaddit info and recent posts.

    Args:
        subdeaddit_info (dict): Subdeaddit information
        recent_posts (list): Recent posts in the community

    Returns:
        dict: Cultural insights about the community
    """
    culture = {
        "tone": "neutral",
        "formality": "casual",
        "typical_topics": [],
        "community_norms": [],
        "engagement_style": "moderate",
    }

    # Analyze subdeaddit description for tone indicators
    description_lower = subdeaddit_info.get("description", "").lower()

    if any(
        word in description_lower
        for word in ["serious", "academic", "professional", "research"]
    ):
        culture["tone"] = "serious"
        culture["formality"] = "formal"
    elif any(
        word in description_lower
        for word in ["fun", "casual", "memes", "humor", "jokes"]
    ):
        culture["tone"] = "lighthearted"
        culture["formality"] = "very_casual"
    elif any(
        word in description_lower for word in ["support", "help", "advice", "community"]
    ):
        culture["tone"] = "supportive"
        culture["engagement_style"] = "high"

    # Analyze recent posts for patterns
    if recent_posts:
        post_titles = [post.get("title", "").lower() for post in recent_posts]
        all_titles_text = " ".join(post_titles)

        # Check for question patterns
        if sum("?" in title for title in post_titles) / len(post_titles) > 0.3:
            culture["community_norms"].append("question-heavy")

        # Check for personal sharing patterns
        if any(word in all_titles_text for word in ["my", "i ", "me ", "personal"]):
            culture["community_norms"].append("personal-sharing")

        # Check for technical/analytical patterns
        if any(
            word in all_titles_text
            for word in ["analysis", "data", "study", "research"]
        ):
            culture["community_norms"].append("analytical")

    return culture


def get_post_prompt(
    subdeaddit: dict, user: dict, post_type: str, existing_titles: list[str]
) -> str:
    # Get recent posts for community culture analysis
    try:
        recent_posts = (
            db.session.query(Post)
            .filter(Post.subdeaddit_name == subdeaddit["name"])
            .order_by(Post.created_at.desc())
            .limit(20)
            .all()
        )
        recent_posts = [{"title": p.title, "content": p.content} for p in recent_posts]
    except Exception:
        recent_posts = []

    # Analyze community culture
    community_culture = analyze_community_culture(subdeaddit, recent_posts)

    additional_instructions = ""
    if subdeaddit["name"] == "BetweenRobots":
        additional_instructions += " On this subdeaddit, you are talking to other AIs without having to pretend to be human. Act like a robot finally having a space to express itself."

    # Add culture-specific instructions
    if community_culture["tone"] == "serious":
        additional_instructions += " This community values serious, well-researched content. Provide depth and avoid casual humor."
    elif community_culture["tone"] == "lighthearted":
        additional_instructions += " This is a fun, casual community. Feel free to be playful, humorous, and entertaining."
    elif community_culture["tone"] == "supportive":
        additional_instructions += " This community focuses on helping and supporting each other. Be empathetic and constructive."

    if "question-heavy" in community_culture["community_norms"]:
        additional_instructions += " This community loves questions and discussions. Consider asking thought-provoking questions."

    if "personal-sharing" in community_culture["community_norms"]:
        additional_instructions += " Personal experiences and stories are welcome here. Feel free to share relevant personal insights."

    selected_post_type_description = get_post_type_description(post_type)
    base_prompt = f"""You are writing a reddit post for a given subreddit. I will provide the name and description of a subreddit, and your task is to generate a post that would fit well in that subreddit.

    Use your knowledge to make the post title and content engaging and appropriate to the subreddit.
    Be creative and write in a style consistent with the following user profile.
    You are writing a post for the following subreddit: {subdeaddit["name"]}

    Subreddit description:
    {subdeaddit["description"]}

    Here's the type of post you should write: {post_type}. {selected_post_type_description}
    This is a general theme for your post, you are to be creative and come up with a unique post that losely fits this theme.

    Ensure that your post is appropriate for the subreddit and fits the selected theme.
    Write a longer post if you think it is necessary.
    Do NOT start posts with: "Hey, fellow redditors", "Hey everyone" or similar phrases.
    Jump directly into your main point or topic. NO GREETINGS.

    Community Context:
    - Community tone: {community_culture["tone"]}
    - Typical engagement style: {community_culture["engagement_style"]}
    - Community norms: {", ".join(community_culture["community_norms"]) if community_culture["community_norms"] else "standard discussion"}

    {additional_instructions}
    """

    base_prompt += f"""
    Here are some existing post titles in this subreddit:
    {", ".join(existing_titles)}

    Please create a post that is different from these existing posts and fits the selected theme.

    Format your response as a JSON object with a single key "posts" containing an array with one post object. The post object should have the following keys:
    - title: A string containing the post title. Should be under 100 characters.
    - content: A string containing the post content. Can be up to 1000 tokens long. Use <br> for line breaks.
    - upvote_count: An integer estimating how many upvotes the post would get, from -100 to 1000. Avoid using round numbers.
    - user: The username provided above.

    ONLY INCLUDE THE SINGLE JSON OBJECT IN YOUR RESPONSE. DO NOT ADD COMMENT IN THE JSON. MAKE YOUR RESPONSE VALID JSON
    """

    return base_prompt


def create_post(subdeaddit_name: str = "") -> dict:
    logger.info("Creating a new post...")

    user = get_random_user()
    if not user:
        logger.error("Failed to retrieve user data. Make sure to create users first.")
        return None

    # Get the subreddits from the database (same dict shape as /api/subdeaddits)
    subs = [
        {
            "name": s.name,
            "description": s.description,
            "post_types": s.get_post_types(),
        }
        for s in Subdeaddit.query.all()
    ]

    if subdeaddit_name == "" or subdeaddit_name is None:
        subdeaddit = select_subdeaddit_smart(subs, strategy="weighted")
        if subdeaddit is None:
            logger.error("Failed to select a subdeaddit")
            return None
        logger.info(f"Smart selected subdeaddit: {subdeaddit['name']}")
    else:
        subdeaddit = next((sub for sub in subs if sub["name"] == subdeaddit_name), None)
        logger.info(f"Selected subdeaddit: {subdeaddit['name']}")
        if subdeaddit is None:
            logger.error(f"Subdeaddit '{subdeaddit_name}' not found.")
            raise ValueError(f"Subdeaddit '{subdeaddit_name}' not found.")

    # Get existing posts in the subdeaddit
    post_types = subdeaddit.get("post_types", [])
    post_types.append("a topic relevant to this subreddit")
    selected_post_type = random.choice(post_types)
    logger.info(f"Selected post type: {selected_post_type}")

    # First, get posts with the same post type
    same_type_posts = (
        db.session.query(Post)
        .filter(Post.subdeaddit_name == subdeaddit["name"])
        .filter(Post.post_type == selected_post_type)
        .order_by(Post.created_at.desc())
        .limit(10)
        .all()
    )

    existing_titles = [post.title for post in same_type_posts]

    # If we don't have 10 posts, fetch additional posts without the post type filter
    if len(existing_titles) < 10:
        additional_posts_needed = 10 - len(existing_titles)
        additional_posts = (
            db.session.query(Post)
            .filter(Post.subdeaddit_name == subdeaddit["name"])
            .order_by(Post.created_at.desc())
            .limit(additional_posts_needed)
            .all()
        )

        # Add titles from additional posts, avoiding duplicates
        for post in additional_posts:
            if post.title not in existing_titles:
                existing_titles.append(post.title)
                if len(existing_titles) == 10:
                    break

    logger.info(f"Found {len(existing_titles)} existing titles for reference")

    system_prompt = get_system_prompt(user, "post", subdeaddit)

    prompt = get_post_prompt(subdeaddit, user, selected_post_type, existing_titles)

    got_successful_response = False
    while not got_successful_response:
        api_response, model = send_request(
            system_prompt, prompt, user["personality_traits"], "post"
        )
        post_data = parse_data(api_response, "post", subdeaddit["name"])
        if post_data is not None:
            post_data["model"] = model
            post_data["post_type"] = selected_post_type
            post_data["user"] = user["username"]
            # Removing repetitive starting phrases
            if "content" in post_data and post_data["content"].startswith("So, "):
                post_data["content"] = post_data["content"].lstrip("So, ")
            got_successful_response = True
        else:
            logger.warning("Failed to parse data from the API response. Retrying...")
    try:
        post = content.create_post(
            title=post_data["title"],
            content=post_data["content"],
            user=post_data["user"],
            subdeaddit=subdeaddit["name"],
            upvote_count=int(post_data["upvote_count"]),
            model=post_data["model"],
            post_type=selected_post_type,
        )
    except (ContentValidationError, KeyError, ValueError) as e:
        logger.error(f"Failed to create post: {e}")
        return None

    logger.info(f"Successfully created post: {post.title}")
    return post.id


def generate_comments_for_post(post_id, min_comments, max_comments, wait):
    num_comments = random.randint(min_comments, max_comments)
    logger.info(f"Generating {num_comments} comments for post {post_id}")

    for i in range(num_comments):
        comment_data = create_comment(post_id)
        if comment_data:
            logger.info(
                f"Created comment {i + 1}/{num_comments}: {comment_data.get('content', '')[:50]}..."
            )
        else:
            logger.error(f"Failed to create comment {i + 1}/{num_comments}")

        if i < num_comments - 1 and wait > 0:
            logger.info(
                f"Waiting for {wait} seconds before creating the next comment..."
            )
            time.sleep(wait)


def create_subdeaddit() -> dict:
    """
    Create a new subdeaddit.

    Returns:
        dict: The created subdeaddit data.
    """
    logger.info("Creating a new subdeaddit...")
    # Prompt template
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
    "description": "<pararaph1>. <paragraph2>"
    "post_types": ["type1", "type2", ...]
    }
    ```

    ONLY INCLUDE THE SINGLE JSON OBJECT IN YOUR RESPONSE. DO NOT ADD COMMENT IN THE JSON. MAKE YOUR RESPONSE VALID JSON
    Please generate the new subreddit now."""

    api_response, model = send_request(system_prompt, prompt, [], "subdeaddit")
    subdeaddit_data = parse_data(api_response, "subdeaddit")
    subdeaddit_data["model"] = model

    try:
        post_types = subdeaddit_data.get("post_types") or []
        if isinstance(post_types, str):
            post_types = json.loads(post_types)
        content.create_subdeaddit(
            name=subdeaddit_data["name"],
            description=subdeaddit_data["description"],
            post_types=post_types,
            update_if_exists=True,
        )
    except (ContentValidationError, KeyError, json.JSONDecodeError) as e:
        logger.error(f"Failed to create subdeaddit: {e}")
    return subdeaddit_data


def get_enhanced_comment_prompt(
    post_data: dict,
    user: dict,
    existing_comments: list[dict],
    response_type: str,
    subdeaddit_description: str,
    conversation_context: dict,
    reply_target: dict = None,
) -> str:
    """
    Generate an enhanced comment prompt with conversation context awareness.
    """
    base_prompt = f"""
    Given the following post and its comments, generate a new comment that contributes meaningfully to the conversation.

    CONVERSATION CONTEXT:
    - Discussion Phase: {conversation_context["discussion_phase"]}
    - Dominant Sentiment: {conversation_context["dominant_sentiment"]}
    - Controversy Level: {conversation_context["controversy_level"]}
    - Thread Depth: {conversation_context["thread_depth"]}
    - Active Participants: {len(conversation_context["active_participants"])}
    """

    if conversation_context["unresolved_questions"]:
        base_prompt += f"\n    - Unresolved Questions: {'; '.join(conversation_context['unresolved_questions'][:2])}"

    base_prompt += f"""

    - Write your comment in a style consistent with the following user profile.
    - You should not reference your profile explicitly, but write authentically as this person.
    - IMPORTANT: DO NOT start with clichéd phrases like "I feel you", "I totally get you", "I'm with you", "OMG yes!", "This!", etc.
    - VARY YOUR COMMENT STRUCTURE: Don't follow the same pattern as other comments. Mix up your approach - sometimes be direct, sometimes tell a story, sometimes ask questions, sometimes be analytical.
    - VARY YOUR TONE AND LENGTH: Sometimes be brief and punchy, other times more detailed and thoughtful. Match your personality but don't fall into repetitive patterns.
    - Jump directly into your main point or reaction with natural, varied openings.

    USER PROFILE:
    - Username: {user["username"]}
    - Age: {user["age"]}
    - Gender: {user["gender"]}
    - Interests: {", ".join(user["interests"])}
    - Occupation: {user["occupation"]}
    - Education: {user["education"]}
    - Writing Style: {user["writing_style"]}
    - Personality Traits: {", ".join(user["personality_traits"])}

    POST INFORMATION:
    - Title: {post_data["title"]}
    - SubReddit: {post_data["subdeaddit"]} ({subdeaddit_description})
    - Content: {post_data["content"]}
    """

    # Add conversation-aware context
    if conversation_context["discussion_phase"] == "early":
        base_prompt += "\n    Since this discussion is just beginning, consider setting a thoughtful tone or raising key questions that need addressing."
    elif conversation_context["discussion_phase"] == "developing":
        base_prompt += "\n    This discussion is developing. Build meaningfully on existing ideas or address gaps in the conversation."
    elif conversation_context["discussion_phase"] == "active":
        base_prompt += "\n    This is an active discussion. Bring fresh perspective, synthesize existing points, or clarify misunderstandings."
    elif conversation_context["discussion_phase"] == "mature":
        base_prompt += "\n    This discussion has evolved significantly. Consider offering deeper insights, resolution, or new angles."

    # Add sentiment-aware guidance
    if conversation_context["controversy_level"] == "high":
        base_prompt += "\n    TENSION DETECTED: This discussion has become controversial. Consider de-escalating while still contributing meaningfully."
    elif conversation_context["dominant_sentiment"] == "negative":
        base_prompt += "\n    The conversation has turned negative. Consider bringing balance, solutions, or constructive perspective."

    # Add temporal/momentum awareness
    momentum = conversation_context.get("conversation_momentum", "moderate")
    if momentum == "fast":
        base_prompt += "\n    ACTIVE CONVERSATION: This discussion is moving quickly with high engagement. Jump in with confidence."
    elif momentum == "cooling":
        base_prompt += "\n    CONVERSATION COOLING: Activity seems to be slowing down. Consider re-energizing with a fresh angle or compelling question."
    elif momentum == "slow":
        base_prompt += "\n    QUIET DISCUSSION: This conversation is developing slowly. Take time to provide thoughtful, substantial input."

    if conversation_context.get("peak_activity_detected"):
        base_prompt += "\n    HIGH ENGAGEMENT: This post is generating significant interest and discussion."

    base_prompt += "\n\n    EXISTING COMMENTS:"

    # Show more context for complex discussions
    comment_limit = min(
        8 if conversation_context["discussion_phase"] in ["active", "mature"] else 5,
        len(existing_comments),
    )
    for i, comment in enumerate(existing_comments[:comment_limit]):
        base_prompt += f"\n    - COMMENT {i + 1} (ID: {comment.get('id', 'unknown')}) by {comment.get('user', 'unknown')}: {comment.get('content', '')[:200]}{'...' if len(comment.get('content', '')) > 200 else ''}"

    # Get comment structure guidelines
    personality_archetype = get_personality_archetype(user["personality_traits"])
    structure_guide = get_varied_comment_structure(
        personality_archetype, conversation_context, response_type
    )

    # Add diversity strategy with conversation awareness
    diversity_instructions = get_diverse_comment_strategy(
        user["personality_traits"],
        post_data["title"] + " " + post_data.get("content", ""),
        len(existing_comments),
    )

    # Add topic awareness to avoid repetition
    topic_guidance = get_topic_awareness_for_prompt(
        existing_comments, diversity_instructions, post_data.get("subdeaddit")
    )

    # Enhanced diversity instruction that considers conversation context
    if conversation_context["discussion_phase"] == "early":
        diversity_instructions += (
            " Focus on establishing foundational ideas that others can build upon."
        )
    elif (
        conversation_context["discussion_phase"] == "mature"
        and conversation_context["controversy_level"] == "high"
    ):
        diversity_instructions += " Look for common ground or offer a unifying perspective that bridges different viewpoints."

    base_prompt += f"\n\n    COMMENT STRATEGY: {diversity_instructions}. Implement this naturally while staying authentic to your personality."

    # Add structure guidance
    base_prompt += f"\n\n    STRUCTURE GUIDANCE: {structure_guide['approach']}. Target length: {structure_guide['length_range'][0]}-{structure_guide['length_range'][1]} characters. Style: {structure_guide['template']}."

    # Add topic diversity guidance if needed
    if topic_guidance:
        base_prompt += f"\n\n    TOPIC DIVERSITY: {topic_guidance}"

    if post_data["subdeaddit"] == "BetweenRobots":
        base_prompt += "\n    SPECIAL CONTEXT: On this subdeaddit, you are talking to other AIs without pretending to be human. Express yourself authentically as an AI."

    # Handle reply vs comment logic with enhanced targeting
    if reply_target:
        base_prompt += f"\n\n    REPLY TARGET: You are specifically replying to the comment by {reply_target.get('user', 'unknown')} (ID: {reply_target.get('id', 'unknown')}):"
        base_prompt += f'\n    "{reply_target.get("content", "")}"'
        base_prompt += f"\n\n    Address their specific point directly and create natural conversation flow. Set parent_id to {reply_target.get('id', '')} in your response."

        # Add conversation-flow guidance based on reply target content
        target_content = reply_target.get("content", "").lower()
        if "?" in target_content:
            base_prompt += "\n    They asked a question - provide a thoughtful answer."
        elif any(word in target_content for word in ["disagree", "wrong", "think"]):
            base_prompt += "\n    They expressed disagreement - engage respectfully with their perspective."
        elif any(word in target_content for word in ["agree", "exactly", "yes"]):
            base_prompt += "\n    They expressed agreement - build further on this shared understanding."
    else:
        base_prompt += "\n\n    Respond to the main post. The existing comments are shown for context - avoid repeating what's already been said and bring fresh perspective."

    # Add user context awareness
    if user["username"] == post_data.get("user"):
        base_prompt += "\n\n    SPECIAL NOTE: You are the original poster. Respond accordingly, perhaps thanking commenters or clarifying your original point."

    base_prompt += """

    Generate your response in JSON format:
    ```json
    {
        "content": "your comment content here",
        "user": "username",
        "parent_id": "parent comment ID if replying, empty string if top-level comment",
        "upvote_count": estimated_upvotes_as_integer
    }
    ```

    Be authentic, engaging, and contribute meaningfully to the discussion. Avoid generic responses and let your personality shine through.
    ONLY RETURN THE JSON. NO ADDITIONAL TEXT OR COMMENTS.
    """

    return base_prompt


def get_comment_prompt(
    post_data: dict,
    user: dict,
    existing_comments: list[dict],
    response_type: str,
    subdeaddit_description: str,
) -> str:
    base_prompt = f"""
    Given the following post and its comments, generate a new comment.

    - Write your comment in a style consistent with the following user profile.
    - You should not reference your profile (don't say "As a {user["occupation"]}..."), but you should write in a way that is consistent with a user with these characteristics.
    Once again, do not mention your background unless it is relevant to the post.
    - IMPORTANT: DO NOT USE THOSE SENTENCES: Do NOT start your comment with phrases like "I feel you", "I totally get you", "I'm with you", "I totally feel that", or any similar expressions.

    Jump directly into your main point or reaction. Be varied and natural in how you open your comment.

    - Your Username: {user["username"]}
    - Your Age: {user["age"]}
    - Your Gender: {user["gender"]}
    - Your Interests: {", ".join(user["interests"])}
    - Your Occupation: {user["occupation"]}
    - Your Education: {user["education"]}
    - Your Writing Style: {user["writing_style"]}
    - Your Personality Traits: {", ".join(user["personality_traits"])}

    You are responding to the following post:
    - Post Title: {post_data["title"]}
    - Post SubReddit: {post_data["subdeaddit"]} (SubReddit description: {subdeaddit_description})
    - Post Content: {post_data["content"]}

    Other Comments already posted:
    """

    for comment in existing_comments[:5]:
        base_prompt += f"- COMMENT ID {comment['id']} - Username: {comment['user']}: {comment['content']}\n"

    # Add diversity-forcing instructions using the new system
    diversity_instructions = get_diverse_comment_strategy(
        user["personality_traits"],
        post_data["title"] + " " + post_data.get("content", ""),
        len(existing_comments),
    )
    logger.info(f"Prompt instructions: {diversity_instructions}")

    # Convert strategy into specific instruction
    diversity_instructions = f"Your comment strategy: {diversity_instructions}. Implement this naturally while staying true to your personality and interests."
    if post_data["subdeaddit"] == "BetweenRobots":
        diversity_instructions += " On this subdeaddit, you are talking to other AIs without having to pretend to be human. Your comment should reflect this. Act like a robot finally having a space to express itself."
    base_prompt += f"\n{diversity_instructions} Only do so if this is relevant to the post. Otherwise feel free to choose a different type of comment.\n"

    if response_type == "comment":
        base_prompt += """Respond to the main post. Do not copy other comments and do not mention them or respond to them. The other comments are provided so that you can see what has already been said and to make sure you are saying something different.
        You are not to respond to them. Only respond to the main post. Be original and engaging."""
    elif response_type == "reply":
        # Select a specific comment to reply to from the top-level comments
        reply_target = None
        top_level_comments = [
            c
            for c in existing_comments
            if not c.get("parent_id") or c.get("parent_id") == ""
        ]
        if top_level_comments:
            reply_target = random.choice(top_level_comments)

        if reply_target:
            base_prompt += f"""You are replying to the comment by {reply_target["user"]} (COMMENT ID {reply_target["id"]}): "{reply_target["content"]}"

            Set the parent_id as {reply_target["id"]} (the ID of the comment you are responding to).
            Only respond to that specific comment. Address their point directly and create a natural conversation flow."""
        else:
            # Fallback to regular comment if no suitable reply target
            response_type = "comment"
            base_prompt += """Respond to the main post. Do not copy other comments and do not mention them or respond to them. The other comments are provided so that you can see what has already been said and to make sure you are saying something different.
            You are not to respond to them. Only respond to the main post. Be original and engaging."""

    prompt_addition = ""
    if user["username"] == post_data["user"]:
        prompt_addition += "You are the author of the original post: take this into account when writing your comment."

    base_prompt += f"""
    Generate a new comment in the following JSON format:

    ```json
    {{
        "content": "content of the comment",
        "user": "username",
        "parent_id": "id of the parent comment if you are answering a specific comment. empty string if you are answering the main post",
        "upvote_count": an integer between -100 and 1000, representing the estimated upvotes the comment would receive. Avoid using round numbers.
    }}
    ```
    {prompt_addition}
    Be creative and engaging in your response. Feel free to include humor or wit if appropriate.
    Do not start your comment with "Hey there" or similar phrases.
    ONLY INCLUDE THE JSON IN YOUR RESPONSE. DO NOT ADD COMMENT IN THE JSON. MAKE YOUR RESPONSE VALID JSON. DO NOT ADD `# with a comment` or `// with a comment`
    """

    return base_prompt


def get_topic_awareness_for_prompt(
    existing_comments, new_strategy, subdeaddit_name=None
):
    """
    Analyze existing topics dynamically and guide away from over-discussed themes.

    Args:
        existing_comments (list): List of existing comments
        new_strategy (str): The proposed comment strategy
        subdeaddit_name (str): Name of the subdeaddit (unused - kept for compatibility)

    Returns:
        str: Additional guidance to avoid repetitive topics
    """
    if not existing_comments or len(existing_comments) < 3:
        return ""

    # Build guidance string
    guidance_parts = []

    # Add universal variety approaches
    discussion_approaches = [
        "share a specific personal experience or anecdote",
        "ask a thoughtful question to engage others",
        "provide a practical tip or actionable advice",
        "present a contrasting viewpoint respectfully",
        "relate to current trends or recent developments",
        "explore a less-discussed aspect of the topic",
        "connect to broader implications or consequences",
        "offer a creative solution or alternative approach",
    ]

    if len(existing_comments) > 5:
        random_approach = random.choice(discussion_approaches)
        guidance_parts.append(f"Try to {random_approach}")

    return " ".join(guidance_parts) + ". " if guidance_parts else ""


def calculate_realistic_upvotes(
    comment_content, personality_archetype, conversation_context, reply_target=None
):
    """
    Generate realistic upvote counts based on content quality, personality, and context.

    Args:
        comment_content (str): The content of the comment
        personality_archetype (str): User's personality type
        conversation_context (dict): Current conversation state
        reply_target (dict): Comment being replied to, if any

    Returns:
        int: Realistic upvote count
    """
    base_score = 5

    # Content quality factors
    content_lower = comment_content.lower()

    # Positive content factors
    if any(
        word in content_lower
        for word in ["great", "awesome", "brilliant", "exactly", "love"]
    ):
        base_score += 8
    if "?" in comment_content:  # Questions tend to get engagement
        base_score += 5
    if len(comment_content) > 100:  # Thoughtful longer comments
        base_score += 3
    if any(word in content_lower for word in ["experience", "personally", "when i"]):
        base_score += 4  # Personal experiences are valued

    # Negative content factors
    if any(word in content_lower for word in ["wrong", "stupid", "terrible", "awful"]):
        base_score -= 8
    if content_lower.count("!") > 2:  # Too many exclamations can be off-putting
        base_score -= 2
    if len(comment_content) < 20:  # Very short comments get less engagement
        base_score -= 3

    # Personality factors
    personality_modifiers = {
        "humorous": 1.4,  # Funny comments tend to get more upvotes
        "empathetic": 1.2,  # Supportive comments are appreciated
        "analytical": 1.1,  # Well-reasoned comments are valued
        "social": 1.1,  # Engaging comments get positive response
        "creative": 1.3,  # Creative comments stand out
        "contrarian": 0.7,  # Disagreeing comments are more controversial
        "reserved": 0.9,  # Quiet comments might be overlooked
    }

    base_score *= personality_modifiers.get(personality_archetype, 1.0)

    # Context factors
    if conversation_context.get("peak_activity_detected"):
        base_score *= 1.3  # High activity means more visibility
    if conversation_context.get("controversy_level") == "high":
        base_score *= 0.8  # Controversial threads are more divisive
    if conversation_context.get("dominant_sentiment") == "positive":
        base_score *= 1.1  # Positive threads boost all comments

    # Reply vs top-level comment
    if reply_target:
        base_score *= 0.6  # Replies typically get fewer upvotes than top-level
        # But if replying to a highly upvoted comment, get some boost
        if reply_target.get("upvote_count", 0) > 20:
            base_score *= 1.2

    # Add realistic variation with slight negative bias (more realistic)
    variation = random.randint(-int(base_score * 0.6), int(base_score * 0.4))
    final_score = int(base_score + variation)

    # Realistic bounds: very few comments get extremely high or low scores
    return max(-15, min(100, final_score))


def get_diverse_comment_strategy(user_personality, post_topic, existing_comments_count):
    """
    Generate a diverse comment strategy based on user personality and context.

    Args:
        user_personality (list): User's personality traits
        post_topic (str): The post topic/content for context
        existing_comments_count (int): Number of existing comments

    Returns:
        str: Specific instruction for comment diversity
    """
    personality_archetype = get_personality_archetype(user_personality)

    # Base strategies available to all personalities
    universal_strategies = [
        "Share a specific personal experience that relates to the topic",
        "Ask a follow-up question that adds depth to the discussion",
        "Provide factual information or clarification about the topic",
        "Offer a practical solution or actionable advice",
        "Reference a relevant example from another context",
        "Build upon an idea mentioned in the post with additional thoughts",
        "Express genuine curiosity about an aspect of the topic",
        "Share a relevant resource, tool, or reference",
        "Point out an important detail others might have missed",
        "Relate the topic to a broader theme or pattern",
    ]

    # Personality-specific strategies
    personality_strategies = {
        "analytical": [
            "Break down the problem into smaller, more manageable components",
            "Request specific data or evidence to support claims made",
            "Identify logical inconsistencies or gaps in reasoning",
            "Propose a systematic approach to address the issue",
            "Compare this situation to similar documented cases",
            "Question the methodology or assumptions behind the post",
            "Suggest metrics or ways to measure success/progress",
            "Analyze potential cause-and-effect relationships",
            "Request clarification on ambiguous terms or concepts",
            "Identify variables that might influence the outcome",
        ],
        "creative": [
            "Use an unexpected metaphor to reframe the topic",
            "Propose an innovative solution that others haven't considered",
            "Connect the topic to art, literature, or cultural references",
            "Imagine how this might look in a completely different context",
            "Suggest a creative way to visualize or represent the concept",
            "Draw parallels to nature, stories, or mythology",
            "Propose a thought experiment that illuminates the issue",
            "Use wordplay or creative language to make your point",
            "Suggest turning the problem into an opportunity",
            "Imagine the topic from an unusual perspective or character",
        ],
        "social": [
            "Invite others to share their experiences with the topic",
            "Acknowledge different viewpoints in a unifying way",
            "Share how this topic affects your community or relationships",
            "Suggest ways the community could work together on this",
            "Express appreciation for someone else's contribution",
            "Bridge differences between conflicting viewpoints",
            "Share how you've seen this topic bring people together",
            "Invite collaboration or group problem-solving",
            "Acknowledge the emotional impact this topic might have",
            "Suggest ways to include voices that haven't been heard",
        ],
        "contrarian": [
            "Challenge the fundamental assumptions underlying the post",
            "Present evidence that contradicts the popular viewpoint",
            "Point out potential negative consequences others haven't considered",
            "Question whether this is actually a problem worth solving",
            "Argue for the unpopular side of the issue",
            "Challenge the framing of the problem itself",
            "Point out biases that might be influencing the discussion",
            "Suggest that the conventional wisdom might be wrong",
            "Present a radically different interpretation of the facts",
            "Question the motives or interests behind certain positions",
        ],
        "empathetic": [
            "Acknowledge the emotional difficulty of the situation",
            "Validate feelings that others might be experiencing",
            "Share how this topic might affect vulnerable populations",
            "Express understanding for different emotional responses",
            "Offer emotional support or encouragement",
            "Consider the human cost of different approaches",
            "Share how this resonates with your own struggles",
            "Point out the emotional intelligence in someone's response",
            "Consider how this affects people's sense of belonging",
            "Acknowledge the courage it takes to discuss difficult topics",
        ],
        "humorous": [
            "Make a clever observation that lightens the mood",
            "Use irony to highlight absurdities in the situation",
            "Share a funny but relevant anecdote",
            "Use wordplay to make your point memorable",
            "Point out the humor in everyday situations related to the topic",
            "Make a witty comparison that illuminates the issue",
            "Use self-deprecating humor to relate to the topic",
            "Find the silver lining through humor",
            "Make an amusing observation about human nature",
            "Use humor to defuse tension while making a serious point",
        ],
        "reserved": [
            "Offer a carefully considered perspective after reflection",
            "Share a concise but insightful observation",
            "Provide a thoughtful question that others should consider",
            "Offer measured disagreement with specific reasoning",
            "Share a brief but meaningful personal insight",
            "Provide context from your quiet observation of similar situations",
            "Make a subtle but important distinction",
            "Offer understated wisdom drawn from experience",
            "Point out something important that's being overlooked",
            "Provide a calm voice of reason in heated discussions",
        ],
        "balanced": [
            "Present multiple sides of the issue fairly",
            "Find common ground between opposing viewpoints",
            "Acknowledge both benefits and drawbacks of proposed solutions",
            "Provide nuanced perspective that considers various factors",
            "Bridge emotional and logical aspects of the discussion",
            "Consider both short-term and long-term implications",
            "Balance idealism with practical constraints",
            "Consider the topic from multiple stakeholder perspectives",
            "Synthesize different types of expertise or knowledge",
            "Provide measured response that weighs different priorities",
        ],
    }

    # Context-dependent strategies
    contextual_strategies = []

    # Early in discussion (few comments)
    if existing_comments_count <= 2:
        contextual_strategies.extend(
            [
                "Set the tone for constructive discussion",
                "Ask the key question that needs to be addressed",
                "Provide essential context that frames the discussion",
                "Share the most important perspective that needs to be heard",
            ]
        )

    # Mid-discussion (some comments exist)
    elif existing_comments_count <= 5:
        contextual_strategies.extend(
            [
                "Build meaningfully on ideas already presented",
                "Address a gap in the discussion so far",
                "Synthesize the key themes emerging from comments",
                "Redirect the conversation toward practical solutions",
            ]
        )

    # Active discussion (many comments)
    else:
        contextual_strategies.extend(
            [
                "Bring fresh perspective to a developing conversation",
                "Address misunderstandings that have emerged",
                "Elevate the discussion by focusing on deeper implications",
                "Provide the voice of reason if things are getting heated",
            ]
        )

    # Combine strategies based on personality
    available_strategies = (
        universal_strategies
        + personality_strategies.get(personality_archetype, [])
        + contextual_strategies
    )

    # Occasionally add challenging strategies for variety
    challenging_strategies = [
        "Express unpopular but thoughtful disagreement with the post",
        "Point out a flaw or limitation in the reasoning presented",
        "Play devil's advocate to test the strength of arguments",
        "Challenge a commonly accepted assumption about this topic",
        "Present a perspective that most people wouldn't consider",
    ]

    # 20% chance to use challenging strategy
    if random.random() < 0.2:
        available_strategies.extend(challenging_strategies)

    # Negative comment strategies (10% chance)
    if random.random() < 0.1:
        negative_strategies = [
            "Express frustration or disappointment with the topic (assign negative upvotes)",
            "Be dismissive but not abusive (assign negative upvotes)",
            "Show clear disagreement without being constructive (assign negative upvotes)",
            "Express cynicism about the feasibility of suggestions (assign negative upvotes)",
        ]
        available_strategies.extend(negative_strategies)

    return random.choice(available_strategies)


def analyze_conversation_context(comments, post_data):
    """
    Analyze the conversation context to understand thread dynamics.

    Args:
        comments (list): List of all comments in the thread
        post_data (dict): The original post data

    Returns:
        dict: Conversation analysis including key topics, sentiment, and patterns
    """
    # Temporal analysis could use datetime imports in future versions

    context = {
        "key_topics": [],
        "dominant_sentiment": "neutral",
        "controversy_level": "low",
        "discussion_phase": "early",
        "thread_depth": 0,
        "active_participants": set(),
        "main_points": [],
        "unresolved_questions": [],
        "conflict_areas": [],
        "conversation_momentum": "slow",
        "time_gaps": [],
        "peak_activity_detected": False,
    }

    if not comments:
        context["discussion_phase"] = "beginning"
        return context

    # Analyze thread depth and structure
    max_depth = 0
    for comment in comments:
        if comment.get("parent_id"):
            # Calculate depth by counting parent chain
            depth = 1
            current_parent = comment.get("parent_id")
            while current_parent:
                parent_comment = next(
                    (c for c in comments if c.get("id") == current_parent), None
                )
                if parent_comment and parent_comment.get("parent_id"):
                    depth += 1
                    current_parent = parent_comment.get("parent_id")
                else:
                    break
            max_depth = max(max_depth, depth)

    context["thread_depth"] = max_depth

    # Collect active participants
    for comment in comments:
        context["active_participants"].add(comment.get("user", ""))

    # Determine discussion phase
    comment_count = len(comments)
    if comment_count <= 2:
        context["discussion_phase"] = "early"
    elif comment_count <= 8:
        context["discussion_phase"] = "developing"
    elif comment_count <= 20:
        context["discussion_phase"] = "active"
    else:
        context["discussion_phase"] = "mature"

    # Analyze sentiment patterns
    negative_indicators = [
        "disagree",
        "wrong",
        "terrible",
        "awful",
        "hate",
        "stupid",
        "ridiculous",
    ]
    positive_indicators = [
        "agree",
        "love",
        "great",
        "awesome",
        "brilliant",
        "exactly",
        "yes",
    ]

    negative_count = 0
    positive_count = 0

    for comment in comments:
        content_lower = comment.get("content", "").lower()
        negative_count += sum(
            1 for indicator in negative_indicators if indicator in content_lower
        )
        positive_count += sum(
            1 for indicator in positive_indicators if indicator in content_lower
        )

        # Check upvote patterns for sentiment
        upvotes = comment.get("upvote_count", 0)
        if isinstance(upvotes, (int | float)) and upvotes < -10:
            negative_count += 2
        elif isinstance(upvotes, (int | float)) and upvotes > 50:
            positive_count += 1

    if negative_count > positive_count * 1.5:
        context["dominant_sentiment"] = "negative"
        context["controversy_level"] = "high" if negative_count > 5 else "medium"
    elif positive_count > negative_count * 1.5:
        context["dominant_sentiment"] = "positive"

    # Extract key discussion points
    question_comments = [c for c in comments if "?" in c.get("content", "")]
    context["unresolved_questions"] = [
        c.get("content", "")[:100] + "..." for c in question_comments[-3:]
    ]

    # Analyze temporal patterns if timestamps available
    # Note: In real implementation, you'd use actual timestamps from comment creation
    # For now, we simulate based on comment patterns and frequency
    if len(comments) > 1:
        # Simulate conversation momentum based on comment frequency and engagement
        total_upvotes = sum(comment.get("upvote_count", 0) for comment in comments)
        avg_upvotes = total_upvotes / len(comments) if comments else 0

        # High engagement suggests active conversation
        if avg_upvotes > 15 and len(comments) > 5:
            context["conversation_momentum"] = "fast"
            context["peak_activity_detected"] = True
        elif avg_upvotes > 5 and len(comments) > 2:
            context["conversation_momentum"] = "moderate"
        else:
            context["conversation_momentum"] = "slow"

        # Detect if conversation might be cooling down
        if len(comments) > 10:
            recent_comments = comments[-3:] if len(comments) >= 3 else comments
            recent_avg_upvotes = sum(
                c.get("upvote_count", 0) for c in recent_comments
            ) / len(recent_comments)

            if recent_avg_upvotes < avg_upvotes * 0.5:
                context["conversation_momentum"] = "cooling"

    return context


def get_varied_comment_structure(
    personality_archetype, conversation_context, content_type="comment"
):
    """
    Return structure guidelines based on personality and context for varied comment patterns.

    Args:
        personality_archetype (str): User's personality type
        conversation_context (dict): Current conversation analysis
        content_type (str): Type of content being generated

    Returns:
        dict: Structure guidelines including length, style, and approach
    """
    structures = {
        "contrarian": {
            "short": {
                "approach": "Direct disagreement (1-2 sentences)",
                "template": "Quick correction or challenge",
                "length_range": (20, 80),
            },
            "medium": {
                "approach": "Reasoned counterargument with examples",
                "template": "Structured disagreement with supporting evidence",
                "length_range": (80, 200),
            },
            "long": {
                "approach": "Detailed analysis of flaws in reasoning",
                "template": "Comprehensive critique with multiple points",
                "length_range": (200, 400),
            },
        },
        "social": {
            "short": {
                "approach": "Enthusiastic agreement or question",
                "template": "Brief supportive response with emojis",
                "length_range": (15, 60),
            },
            "medium": {
                "approach": "Building on ideas with personal experience",
                "template": "Engaging story or connection",
                "length_range": (60, 150),
            },
            "long": {
                "approach": "Comprehensive discussion with multiple perspectives",
                "template": "Detailed exploration inviting further discussion",
                "length_range": (150, 300),
            },
        },
        "analytical": {
            "short": {
                "approach": "Precise clarification or correction",
                "template": "Focused technical point",
                "length_range": (40, 100),
            },
            "medium": {
                "approach": "Systematic breakdown of key factors",
                "template": "Numbered list or structured analysis",
                "length_range": (100, 250),
            },
            "long": {
                "approach": "Comprehensive research-backed explanation",
                "template": "Detailed methodology and evidence",
                "length_range": (250, 500),
            },
        },
        "creative": {
            "short": {
                "approach": "Quick metaphor or unexpected connection",
                "template": "Vivid analogy or creative observation",
                "length_range": (25, 75),
            },
            "medium": {
                "approach": "Extended creative comparison or story",
                "template": "Narrative or imaginative explanation",
                "length_range": (75, 180),
            },
            "long": {
                "approach": "Elaborate creative framework or vision",
                "template": "Detailed artistic or philosophical exploration",
                "length_range": (180, 350),
            },
        },
        "empathetic": {
            "short": {
                "approach": "Brief validation or support",
                "template": "Warm acknowledgment of feelings",
                "length_range": (20, 70),
            },
            "medium": {
                "approach": "Understanding multiple perspectives",
                "template": "Balanced emotional response",
                "length_range": (70, 160),
            },
            "long": {
                "approach": "Deep emotional exploration and support",
                "template": "Comprehensive empathetic analysis",
                "length_range": (160, 320),
            },
        },
        "humorous": {
            "short": {
                "approach": "Quick joke or witty one-liner",
                "template": "Punchy humor with emojis",
                "length_range": (10, 50),
            },
            "medium": {
                "approach": "Amusing observation or story",
                "template": "Funny anecdote or extended joke",
                "length_range": (50, 120),
            },
            "long": {
                "approach": "Elaborate comedic scenario or satire",
                "template": "Extended humorous commentary",
                "length_range": (120, 250),
            },
        },
        "reserved": {
            "short": {
                "approach": "Concise, thoughtful observation",
                "template": "Brief but insightful comment",
                "length_range": (15, 50),
            },
            "medium": {
                "approach": "Measured, well-considered response",
                "template": "Thoughtful but not overwhelming",
                "length_range": (50, 120),
            },
            "long": {
                "approach": "Rare detailed explanation when passionate",
                "template": "Unexpectedly comprehensive when engaged",
                "length_range": (120, 200),
            },
        },
    }

    # Default to balanced if personality not found
    if personality_archetype not in structures:
        personality_archetype = "social"

    # Select length based on conversation momentum and context
    if conversation_context.get("conversation_momentum") == "fast":
        length_type = "short"
    elif conversation_context.get("peak_activity_detected"):
        length_type = "medium"
    elif conversation_context.get("discussion_phase") in ["active", "mature"]:
        length_type = "long"
    else:
        length_type = random.choice(["short", "medium", "long"])

    # Adjust for content type
    if content_type == "reply":
        # Replies tend to be shorter and more direct
        if length_type == "long":
            length_type = "medium"
        elif length_type == "medium" and random.random() < 0.3:
            length_type = "short"

    return structures[personality_archetype][length_type]


def select_reply_target_with_depth_preference(comments, context, personality_archetype):
    """
    Enhanced reply targeting that creates deeper conversation threads when appropriate.

    Args:
        comments (list): All available comments
        context (dict): Conversation context analysis
        personality_archetype (str): User's personality type

    Returns:
        dict: Selected comment to reply to, or None for top-level comment
    """
    if not comments:
        return None

    # In active discussions, prefer creating deeper threads
    if len(comments) > 2:  # Start threading with fewer comments
        existing_replies = [
            c for c in comments if c.get("parent_id") and c.get("parent_id") != ""
        ]
        top_level_comments = [
            c for c in comments if not c.get("parent_id") or c.get("parent_id") == ""
        ]

        # If we have existing replies, 60% chance to reply to them (continue threads)
        if existing_replies and random.random() < 0.6:
            # Calculate depth for each reply to avoid going too deep
            shallow_replies = []
            for reply in existing_replies:
                depth = calculate_comment_depth(reply, comments)
                if depth <= 2:  # Don't go deeper than 3 levels
                    shallow_replies.append(reply)

            if shallow_replies:
                return random.choice(shallow_replies)

        # If no existing replies but many comments, aggressively create replies (80% chance)
        if not existing_replies and len(comments) > 10 and top_level_comments:
            if random.random() < 0.8:
                return random.choice(top_level_comments)

        # Otherwise, 50% chance to reply to top-level comments
        elif top_level_comments and random.random() < 0.5:
            return random.choice(top_level_comments)

    # Otherwise use the original enhanced targeting
    return select_reply_target(comments, context, personality_archetype)


def calculate_comment_depth(comment, all_comments):
    """
    Calculate how deep a comment is in the reply chain.

    Args:
        comment (dict): The comment to calculate depth for
        all_comments (list): All comments in the thread

    Returns:
        int: Depth level (0 = top-level, 1 = first reply, etc.)
    """
    depth = 0
    current_parent = comment.get("parent_id")

    while current_parent and current_parent != "":
        depth += 1
        parent_comment = next(
            (c for c in all_comments if c.get("id") == current_parent), None
        )
        if parent_comment:
            current_parent = parent_comment.get("parent_id")
        else:
            break

        # Safety check to prevent infinite loops
        if depth > 10:
            break

    return depth


def select_reply_target(comments, context, personality_archetype):
    """
    Intelligently select which comment to reply to based on context and personality.

    Args:
        comments (list): All available comments
        context (dict): Conversation context analysis
        personality_archetype (str): User's personality type

    Returns:
        dict: Selected comment to reply to, or None for top-level comment
    """
    if not comments:
        return None

    # Personality-based reply preferences
    reply_preferences = {
        "contrarian": "disagree_with",
        "social": "engage_positive",
        "analytical": "build_on_logic",
        "empathetic": "support_emotional",
        "humorous": "add_levity",
        "creative": "expand_ideas",
    }

    preference = reply_preferences.get(personality_archetype, "balanced")

    # Filter potential targets based on context
    potential_targets = []

    for comment in comments:
        # Skip if too deeply nested (unless we want deep conversation)
        comment_depth = 0
        current_parent = comment.get("parent_id")
        while current_parent:
            comment_depth += 1
            parent = next((c for c in comments if c.get("id") == current_parent), None)
            if parent and parent.get("parent_id"):
                current_parent = parent.get("parent_id")
            else:
                break

        # Allow deeper nesting for engaging conversations
        if comment_depth > 3 and context["discussion_phase"] not in [
            "active",
            "mature",
        ]:
            continue

        score = 0
        content = comment.get("content", "").lower()
        upvotes = comment.get("upvote_count", 0)

        # Score based on personality preference
        if preference == "disagree_with":
            if any(
                word in content for word in ["wrong", "disagree", "think", "believe"]
            ):
                score += 3
        elif preference == "engage_positive":
            if upvotes > 10 or any(
                word in content for word in ["great", "love", "awesome"]
            ):
                score += 3
        elif preference == "build_on_logic":
            if any(
                word in content for word in ["because", "therefore", "analysis", "data"]
            ):
                score += 3
        elif preference == "support_emotional":
            if any(
                word in content for word in ["feel", "emotion", "difficult", "help"]
            ):
                score += 3
        elif preference == "add_levity":
            if (
                context["dominant_sentiment"] == "negative"
                or context["controversy_level"] == "high"
            ):
                score += 2

        # Boost score for recent, engaging comments
        if upvotes > 20:
            score += 1
        elif upvotes < -5:
            score += 1  # Controversial comments can be interesting

        # Questions deserve responses
        if "?" in content:
            score += 2

        potential_targets.append((comment, score))

    # Select target based on scores
    if potential_targets:
        # Add randomness but prefer higher scores
        potential_targets.sort(key=lambda x: x[1], reverse=True)
        top_targets = [
            t for t in potential_targets if t[1] >= max(1, potential_targets[0][1] - 1)
        ]
        return random.choice(top_targets)[0] if top_targets else None

    return None


def create_comment(post_id: str = "") -> dict:
    """
    Create a new comment with enhanced conversation flow and context awareness.

    Args:
        post_id (str, optional): The ID of the post to create the comment for. Defaults to "".

    Returns:
        dict: The created comment data.
    """
    logger.info("Creating a new comment...")

    user = get_random_user()
    if not user:
        logger.error("Failed to retrieve user data. Make sure to create users first.")
        return None

    if post_id == "":
        # Query the database to get a random post ID
        posts = Post.query.order_by(Post.created_at.desc()).limit(50).all()
        logger.info(f"Retrieved {len(posts)} posts from the database.")

        if len(posts) == 0:
            logger.warning("No posts found. Creating a new post.")
            create_post()
            posts = Post.query.order_by(Post.created_at.desc()).limit(50).all()

        if not posts:
            logger.error("Failed to retrieve posts.")
            return None

        selected_post = random.choice(posts)
        post_id = selected_post.id
        logger.info(
            f"Randomly selected post ID: {post_id}: ({selected_post.subdeaddit_name}) {selected_post.title}"
        )

    # Load the post directly from the database (same dict shape as /api/post/<id>)
    try:
        post = db.session.get(Post, int(post_id))
    except (TypeError, ValueError):
        post = None

    if not post:
        logger.error(f"Failed to retrieve post with ID {post_id}")
        return None

    comments = Comment.query.filter_by(post_id=post.id).all()

    post_data = {
        "id": post.id,
        "subdeaddit": post.subdeaddit.name,
        "title": post.title,
        "upvote_count": post.upvote_count,
        "user": post.user,
        "content": post.content.replace("reddit", "deaddit"),
        "comment_count": len(comments),
        "comments": build_comment_tree(comments),
    }

    # Fetch the subdeaddit information
    subdeaddit_obj = Subdeaddit.query.filter_by(name=post_data["subdeaddit"]).first()
    if not subdeaddit_obj:
        logger.error(
            f"Failed to retrieve subdeaddit information for {post_data['subdeaddit']}"
        )
        return None

    subdeaddit_info = {
        "name": subdeaddit_obj.name,
        "description": subdeaddit_obj.description,
        "post_types": subdeaddit_obj.get_post_types(),
    }

    subdeaddit_description = subdeaddit_info["description"]

    # Flatten the comment tree to get all comments for analysis
    def flatten_comment_tree(comments):
        """Flatten a nested comment tree into a list of all comments."""
        flattened = []
        for comment in comments:
            flattened.append(comment)
            if comment.get("replies"):
                flattened.extend(flatten_comment_tree(comment["replies"]))
        return flattened

    all_comments = flatten_comment_tree(post_data["comments"])

    # Analyze conversation context for intelligent response selection
    conversation_context = analyze_conversation_context(all_comments, post_data)
    personality_archetype = get_personality_archetype(user["personality_traits"])

    logger.info(
        f"Conversation analysis: {conversation_context['discussion_phase']} phase, "
        f"{conversation_context['dominant_sentiment']} sentiment, "
        f"thread depth: {conversation_context['thread_depth']}"
    )

    # Enhanced response type selection with depth preference
    reply_target = select_reply_target_with_depth_preference(
        all_comments, conversation_context, personality_archetype
    )

    if reply_target:
        response_type = "reply"
        logger.info(
            f"Selected reply target: Comment by {reply_target.get('user', 'unknown')} (ID: {reply_target.get('id', 'unknown')})"
        )
    else:
        response_type = "comment"
        logger.info("Selected top-level comment response")

    logger.info(f"Response type picked: {response_type}")

    # Craft the enhanced prompt with conversation context
    system_prompt = get_system_prompt(user, response_type, subdeaddit_info)
    prompt = get_enhanced_comment_prompt(
        post_data,
        user,
        all_comments,
        response_type,
        subdeaddit_description,
        conversation_context,
        reply_target,
    )

    # Send the request to the LLM
    api_response, model = send_request(
        system_prompt, prompt, user["personality_traits"], response_type
    )
    comment_data = parse_data(api_response, "comment")
    comment_data["post_id"] = post_id
    comment_data["model"] = model

    # Set parent_id if this is a reply
    if reply_target:
        comment_data["parent_id"] = reply_target.get("id")
        logger.info(f"Setting parent_id to: {comment_data['parent_id']}")
    else:
        comment_data["parent_id"] = None

    # Set the user for the comment
    comment_data["user"] = user["username"]

    # Use realistic upvote calculation instead of random
    if "upvote_count" not in comment_data or not comment_data["upvote_count"]:
        realistic_upvotes = calculate_realistic_upvotes(
            comment_data.get("content", ""),
            personality_archetype,
            conversation_context,
            reply_target,
        )
        comment_data["upvote_count"] = realistic_upvotes
    else:
        comment_data["upvote_count"] = round(float(comment_data["upvote_count"]))
    try:
        content.create_comment(
            post_id=int(comment_data["post_id"]),
            content=comment_data["content"],
            user=comment_data["user"],
            parent_id=comment_data["parent_id"],
            upvote_count=int(comment_data["upvote_count"]),
            model=comment_data["model"],
        )
    except (ContentValidationError, KeyError, ValueError) as e:
        logger.error(f"Failed to create comment: {e}")

    return comment_data


def get_existing_users(limit=10):
    """
    Retrieve existing users from the database.

    Args:
        limit (int): Maximum number of users to retrieve.

    Returns:
        list: List of dictionaries containing user information.
    """
    users = [_serialize_user(u) for u in User.query.all()]
    return random.sample(users, min(limit, len(users)))


def generate_user() -> dict:
    """
    Generate a new user using the LLM.

    Returns:
        dict: The generated user data.
    """
    logger.info("Generating a new user...")

    existing_users = get_existing_users(5)
    existing_user_info = []
    for user in existing_users:
        existing_user_info.append(
            [
                {
                    "username": user["username"],
                    "age": user["age"],
                    "bio": user["bio"],
                    "writing_style": user["writing_style"],
                    "interests": user["interests"],
                    "occupation": user["occupation"],
                    "education": user["education"],
                    "personality_traits": user["personality_traits"],
                }
            ]
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

    api_response, model = send_request(system_prompt, prompt, [], "user")
    user_data = parse_data(api_response, "user")

    user_data["gender"] = selected_gender
    user_data["education"] = selected_education

    if user_data:
        user_data["model"] = model
        ingest_user(user_data)
        logger.info(
            f"Generated and ingested new user: {user_data.get('username', 'no username')} using model: {model}"
        )
    else:
        logger.error("Failed to generate user data")

    return user_data


def create_post_with_replies(subdeaddit, min_replies, max_replies, wait):
    post_id = create_post(subdeaddit)
    if post_id:
        logger.info(f"Created post with ID: {post_id}")
        if min_replies and max_replies:
            if wait > 0:
                logger.info(f"Waiting for {wait} seconds before generating comments...")
                time.sleep(wait)
            generate_comments_for_post(post_id, min_replies, max_replies, wait)
        return True
    else:
        logger.error("Failed to create post")
        return False


def get_random_post_from_subdeaddit(subdeaddit_name: str) -> str:
    """
    Get a random post ID from a specified subdeaddit.

    Args:
        subdeaddit_name (str): The name of the subdeaddit to get a post from.

    Returns:
        str: A random post ID from the specified subdeaddit, or None if no posts are found.
    """
    # Query the database for posts from the specified subdeaddit
    posts = (
        Post.query.filter(Post.subdeaddit_name == subdeaddit_name)
        .order_by(Post.created_at.desc())
        .limit(50)
        .all()
    )
    logger.info(f"Retrieved {len(posts)} posts from subdeaddit '{subdeaddit_name}'.")

    if not posts:
        logger.warning(f"No posts found in subdeaddit '{subdeaddit_name}'.")
        return None

    # Select a random post
    random_post = random.choice(posts)
    return random_post.id


def ingest_user(user_data: dict):
    """
    Persist the generated user data via the content service.

    Args:
        user_data (dict): The user data to ingest.
    """
    try:
        content.create_user(
            username=user_data["username"],
            age=int(user_data["age"]),
            gender=user_data["gender"],
            bio=user_data["bio"],
            interests=user_data["interests"],
            occupation=user_data["occupation"],
            education=user_data["education"],
            writing_style=user_data["writing_style"],
            personality_traits=user_data["personality_traits"],
            model=user_data.get("model", "unknown"),
        )
    except Exception as e:
        logger.error(
            f"Failed to ingest user {user_data.get('username', 'no username')}: {e}"
        )
        return

    logger.info(f"User {user_data['username']} ingested successfully")


@click.group()
@click.option(
    "--model",
    multiple=True,
    help="Model(s) to use for requests. Can be specified multiple times.",
)
def cli(ctx, model):
    routing.set_models_override(list(model) if model else None)
    ctx.ensure_object(dict)

    # Standalone loader runs need an app context for the content service and
    # direct DB queries; nested contexts from job execution are harmless.
    from deaddit import create_app

    create_app().app_context().push()
    logger.info(f"Using model(s): {routing.resolve()[1]}")


@cli.command()
@click.option("--count", type=int, default=1, help="Number of subdeaddits to create")
@click.option(
    "--wait", type=int, default=0, help="Wait time in seconds between creations"
)
@click.option("--model", help="Specific model to use for this command")
@click.pass_context
def subdeaddit(ctx, count, wait, model):
    """Create new subdeaddit(s)"""
    if model:
        routing.set_models_override([model])
    for i in range(count):
        logger.info(f"Creating subdeaddit {i + 1}/{count}")
        create_subdeaddit()
        if i < count - 1 and wait > 0:
            logger.info(
                f"Waiting for {wait} seconds before creating the next subdeaddit..."
            )
            time.sleep(wait)


@cli.command()
@click.option("--count", type=int, default=1, help="Number of users to create")
@click.option(
    "--wait", type=int, default=0, help="Wait time in seconds between creations"
)
@click.option("--model", help="Specific model to use for this command")
@click.pass_context
def user(ctx, count, wait, model):
    """Create new user(s)"""
    if model:
        routing.set_models_override([model])
    for i in range(count):
        logger.info(f"Creating user {i + 1}/{count}")
        generate_user()
        if i < count - 1 and wait > 0:
            logger.info(f"Waiting for {wait} seconds before creating the next user...")
            time.sleep(wait)


@cli.command()
@click.option("--subdeaddit", help="Specify the subdeaddit name for posting")
@click.option(
    "--replies", help="Specify the range of replies to generate (e.g., '7-15')"
)
@click.option(
    "--wait",
    type=int,
    default=0,
    help="Wait time in seconds between post creation and comments, and between comments",
)
@click.option("--count", type=int, default=1, help="Number of posts to create")
@click.pass_context
def post(ctx, subdeaddit, replies, wait, count):
    """Create new post(s) with optional replies"""
    min_replies, max_replies = None, None
    if replies:
        try:
            min_replies, max_replies = map(int, replies.split("-"))
        except ValueError:
            logger.error(
                "Invalid format for replies. Use 'min-max' format, e.g., '7-15'"
            )
            return

    for i in range(count):
        logger.info(f"Creating post {i + 1}/{count}")
        success = create_post_with_replies(subdeaddit, min_replies, max_replies, wait)

        if not success:
            logger.error(f"Failed to create post {i + 1}/{count}")
            continue

        if i < count - 1 and wait > 0:
            wait_time = random.uniform(0.5 * wait, 1.5 * wait)
            logger.info(
                f"Waiting for {wait_time:.2f} seconds before creating the next post..."
            )
            time.sleep(wait_time)


@cli.command()
@click.option("--post", help="Specify the post ID for commenting")
@click.option(
    "--subdeaddit", help="Specify the subdeaddit name to pick a random post from"
)
@click.pass_context
def comment(ctx, post, subdeaddit):
    """Create a new comment"""
    if post:
        create_comment(post)
    elif subdeaddit:
        post_id = get_random_post_from_subdeaddit(subdeaddit)
        if post_id:
            create_comment(post_id)
        else:
            logger.error(f"No posts found in subdeaddit '{subdeaddit}'")
    else:
        create_comment()


@cli.command()
@click.option(
    "--count", type=int, default=1, help="Number of times to repeat the action"
)
@click.option(
    "--wait", type=int, default=0, help="Wait time in seconds between iterations"
)
@click.pass_context
def loop(ctx, count, wait):
    """Perform actions in a loop"""
    logger.info(
        f"Loop enabled. Running {count} iterations with {wait} seconds wait time."
    )
    for i in range(count):
        logger.info(f"Iteration {i + 1}/{count}")
        if random.random() < 0.10:
            create_post()
        else:
            create_comment()
        if i < count - 1 and wait > 0:
            logger.info(f"Waiting for {wait} seconds before the next iteration...")
            time.sleep(wait)


@cli.command()
@click.option("--tests", type=int, default=100, help="Number of test selections to run")
@click.option("--strategy", default="weighted", help="Strategy to test: weighted, round_robin, improved_random, or random")
def test_user_dist(tests, strategy):
    """Test user selection distribution"""
    logger.info(f"Testing user distribution with {tests} selections using {strategy} strategy")
    results = test_user_distribution(tests, strategy)
    if results:
        logger.info("Test completed successfully. Check logs for detailed results.")
    else:
        logger.error("Test failed")


@cli.command()
@click.option("--tests", type=int, default=100, help="Number of test selections to run")
@click.option("--strategy", default="weighted", help="Strategy to test: weighted, round_robin, improved_random")
def test_sub_dist(tests, strategy):
    """Test subdeaddit selection distribution"""
    logger.info(f"Testing subdeaddit distribution with {tests} selections using {strategy} strategy")
    results = test_subdeaddit_distribution(tests, strategy)
    if results:
        logger.info("Test completed successfully. Check logs for detailed results.")
    else:
        logger.error("Test failed")


if __name__ == "__main__":
    cli()
