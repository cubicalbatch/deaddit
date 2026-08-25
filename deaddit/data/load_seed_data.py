"""Seed the database with base users and subdeaddits via the content service."""

import json
import os

from deaddit import create_app
from deaddit.services.content import (
    ContentValidationError,
    create_subdeaddit,
    create_user,
)

# Set the paths to your JSON files
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_JSON_FILE = os.path.join(BASE_DIR, "users.json")
SUBDEADDITS_JSON_FILE = os.path.join(BASE_DIR, "subdeaddits_base.json")


def ingest_users(json_file):
    # Read the JSON file
    try:
        with open(json_file) as file:
            data = json.load(file)
    except FileNotFoundError:
        print(f"Error: JSON file '{json_file}' not found.")
        return
    except json.JSONDecodeError:
        print(f"Error: Invalid JSON in file '{json_file}'.")
        return

    # Process each user
    for user in data.get("users", []):
        try:
            create_user(
                username=user["username"],
                age=int(user["age"]),
                gender=user.get("gender", "Male"),
                bio=user["bio"],
                interests=user.get("interests", []),
                occupation=user["occupation"],
                education=user["education"],
                writing_style=user["writing_style"],
                personality_traits=user.get("personality_traits", []),
                model=user.get("model", "unknown"),
            )
        except (KeyError, TypeError, ContentValidationError) as e:
            print(f"Error ingesting user '{user.get('username', 'unknown')}': {e}")
            continue
        print(f"User '{user['username']}' ingested successfully.")

    print("User ingestion process completed.")


def ingest_subdeaddits(json_file):
    # Read the JSON file
    try:
        with open(json_file) as file:
            data = json.load(file)
    except FileNotFoundError:
        print(f"Error: JSON file '{json_file}' not found.")
        return
    except json.JSONDecodeError:
        print(f"Error: Invalid JSON in file '{json_file}'.")
        return

    # Process each subdeaddit (upserted, matching the legacy ingest endpoint)
    for subdeaddit in data.get("subdeaddits", []):
        name = subdeaddit.get("name")
        try:
            create_subdeaddit(
                name=name,
                description=subdeaddit["description"],
                post_types=subdeaddit.get("post_types", []),
                update_if_exists=True,
            )
        except (KeyError, TypeError, ContentValidationError) as e:
            print(f"Error ingesting subdeaddit '{name}': {e}")
            continue
        print(f"Subdeaddit '{name}' ingested successfully.")

    print("Subdeaddit ingestion process completed.")


if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        print("Starting subdeaddit ingestion...")
        ingest_subdeaddits(SUBDEADDITS_JSON_FILE)
        print("\nStarting user ingestion...")
        ingest_users(USERS_JSON_FILE)
