"""
Simplified CLI chat client for token.place
Uses the CryptoClient helper to handle encryption and API communication
"""

import argparse
import sys
from typing import List, Dict

# Import our CryptoClient
from utils.crypto_helpers import CryptoClient


def clear_screen():
    """Clear the terminal screen without spawning a shell.

    Skips output when stdout is not a TTY.
    """
    if sys.stdout.isatty():
        # Use ANSI escape codes to avoid shell injection via os.system
        print("\033[2J\033[H", end="", flush=True)

ROLE_COLORS = {
    "User": "1;34",
    "Assistant": "1;32",
}


def format_message(message: Dict) -> str:
    """Format a message for display."""
    role = message["role"].capitalize()
    color = ROLE_COLORS.get(role, "1;33")
    return f"\033[{color}m{role}: \033[0m{message['content']}"

def display_conversation(messages: List[Dict]):
    """Display the conversation history"""
    clear_screen()
    print("\033[1;36m=== token.place Chat ===\033[0m\n")
    for message in messages:
        print(format_message(message))
    print("\n" + "-" * 50 + "\n")

def chat_loop(client: CryptoClient):
    """Main chat loop for interactive conversation"""
    conversation = []

    # Fetch server public key
    print("Connecting to server...")
    if not client.fetch_server_public_key():
        print("Failed to connect to server. Make sure the relay is running.")
        sys.exit(1)

    print("Connected! Starting chat session.\n")
    display_conversation(conversation)

    try:
        while True:
            # Get user input
            user_message = input("You: ")
            if user_message.lower() in ['exit', 'quit', 'bye']:
                print("Ending chat session.")
                break

            # Add to local conversation
            conversation.append({"role": "user", "content": user_message})
            display_conversation(conversation)

            # Send message and get response
            print("Assistant is thinking...")
            response = client.send_chat_message(conversation)

            if response is None:
                print("Error: Failed to get response from server.")
                continue

            # Update conversation with the full response from server
            conversation = response

            # Display updated conversation
            display_conversation(conversation)

    except (KeyboardInterrupt, EOFError):
        print("\nChat session ended by user.")

    print("Thank you for using token.place!")

def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="token.place simplified CLI client")
    parser.add_argument("--host", default="http://localhost", help="Host address")
    parser.add_argument("--port", type=int, default=5010, help="Relay port")
    parser.add_argument("--message", help="Single message mode: send a message and exit")
    args = parser.parse_args()

    # Create the API URL
    base_url = f"{args.host}:{args.port}"

    # Create a crypto client
    client = CryptoClient(base_url)

    if args.message:
        # Single message mode
        # Avoid logging plaintext user content
        print("Sending message to server...")
        if client.fetch_server_public_key():
            response = client.send_chat_message(args.message)
            if response:
                # Print the assistant's response
                for msg in response:
                    if msg["role"] == "assistant":
                        print(f"\nAssistant: {msg['content']}")
                        break
            else:
                print("Failed to get response.")
        else:
            print("Failed to connect to server.")
    else:
        # Interactive chat mode
        chat_loop(client)

if __name__ == "__main__":  # pragma: no cover
    main()
