#!/usr/bin/env python3
"""
Test script for Discord Action Module REST API

Usage:
    python test_api.py --help
"""

import argparse
import json
import sys

import jwt
import requests


def generate_token(base_url: str, secret_key: str, client_id: str = "test_client") -> str:
    """Generate JWT token using local secret"""
    payload = {"client_id": client_id}
    token = jwt.encode(payload, secret_key, algorithm="HS256")
    return token


def test_health(base_url: str):
    """Test health endpoint"""
    print("\n=== Testing Health Check ===")
    response = requests.get(f"{base_url}/health")
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    return response.status_code == 200


def test_send_message(base_url: str, token: str, channel_id: str):
    """Test send message endpoint"""
    print("\n=== Testing Send Message ===")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {
        "channel_id": channel_id,
        "content": "Test message from Discord Action Module",
    }
    response = requests.post(f"{base_url}/api/v1/message", headers=headers, json=data)
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    return response.status_code == 200


def test_send_embed(base_url: str, token: str, channel_id: str):
    """Test send embed endpoint"""
    print("\n=== Testing Send Embed ===")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {
        "channel_id": channel_id,
        "embed": {
            "title": "Test Embed",
            "description": "This is a test embed from Discord Action Module",
            "color": "FF5733",
            "fields": [
                {"name": "Field 1", "value": "Value 1", "inline": True},
                {"name": "Field 2", "value": "Value 2", "inline": True},
            ],
        },
    }
    response = requests.post(f"{base_url}/api/v1/embed", headers=headers, json=data)
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    return response.status_code == 200


def test_add_reaction(base_url: str, token: str, channel_id: str, message_id: str):
    """Test add reaction endpoint"""
    print("\n=== Testing Add Reaction ===")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"channel_id": channel_id, "message_id": message_id, "emoji": "👍"}
    response = requests.post(f"{base_url}/api/v1/reaction", headers=headers, json=data)
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    return response.status_code == 200


def test_manage_role(base_url: str, token: str, guild_id: str, user_id: str, role_id: str):
    """Test manage role endpoint"""
    print("\n=== Testing Manage Role ===")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {
        "guild_id": guild_id,
        "user_id": user_id,
        "role_id": role_id,
        "action": "add",
    }
    response = requests.post(f"{base_url}/api/v1/role", headers=headers, json=data)
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    return response.status_code == 200


def main():
    parser = argparse.ArgumentParser(description="Test Discord Action Module REST API")
    parser.add_argument(
        "--base-url",
        default="http://localhost:8070",
        help="Base URL of the API (default: http://localhost:8070)",
    )
    parser.add_argument(
        "--secret-key",
        required=True,
        help="64-character secret key for JWT generation",
    )
    parser.add_argument(
        "--channel-id", help="Discord channel ID for testing message operations"
    )
    parser.add_argument(
        "--message-id", help="Discord message ID for testing reaction operations"
    )
    parser.add_argument(
        "--guild-id", help="Discord guild ID for testing role operations"
    )
    parser.add_argument("--user-id", help="Discord user ID for testing role operations")
    parser.add_argument("--role-id", help="Discord role ID for testing role operations")
    parser.add_argument(
        "--test",
        choices=[
            "health",
            "message",
            "embed",
            "reaction",
            "role",
            "all",
        ],
        default="health",
        help="Test to run (default: health)",
    )

    args = parser.parse_args()

    # Test health first
    if not test_health(args.base_url):
        print("\n❌ Health check failed. Is the server running?")
        sys.exit(1)
    print("\n✅ Health check passed")

    if args.test == "health":
        return

    # Generate JWT token
    print("\n=== Generating JWT Token ===")
    token = generate_token(args.base_url, args.secret_key)
    print(f"Token generated: {token[:50]}...")

    # Run tests
    if args.test == "all":
        if args.channel_id:
            test_send_message(args.base_url, token, args.channel_id)
            test_send_embed(args.base_url, token, args.channel_id)
        if args.channel_id and args.message_id:
            test_add_reaction(args.base_url, token, args.channel_id, args.message_id)
        if args.guild_id and args.user_id and args.role_id:
            test_manage_role(
                args.base_url, token, args.guild_id, args.user_id, args.role_id
            )
    elif args.test == "message":
        if not args.channel_id:
            print("❌ --channel-id required for message test")
            sys.exit(1)
        test_send_message(args.base_url, token, args.channel_id)
    elif args.test == "embed":
        if not args.channel_id:
            print("❌ --channel-id required for embed test")
            sys.exit(1)
        test_send_embed(args.base_url, token, args.channel_id)
    elif args.test == "reaction":
        if not args.channel_id or not args.message_id:
            print("❌ --channel-id and --message-id required for reaction test")
            sys.exit(1)
        test_add_reaction(args.base_url, token, args.channel_id, args.message_id)
    elif args.test == "role":
        if not args.guild_id or not args.user_id or not args.role_id:
            print("❌ --guild-id, --user-id, and --role-id required for role test")
            sys.exit(1)
        test_manage_role(args.base_url, token, args.guild_id, args.user_id, args.role_id)


if __name__ == "__main__":
    main()
