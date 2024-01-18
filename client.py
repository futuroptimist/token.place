import requests
import json
import re

# URL of the server running locally
server_url = 'http://localhost/'

# Initialize an empty chat history
chat_history = []

print("Welcome to the Chat Client!")
print("Type your messages and press Enter to send. Type 'exit' to quit.")

while True:
    user_message = input("You: ")

    if user_message.lower() == 'exit':
        break

    chat_history.append({"role": "user", "content": user_message})

    data = {
        "message": user_message,
        "chat_history": chat_history
    }

    try:
        response = requests.post(server_url, json=data)

        if response.status_code == 200:
            response_data = response.json()
            # Extracting the message content from the response
            print("AI:", response_data[-1]["content"]["choices"][0]["message"]["content"])
        else:
            # Handle non-200 responses
            print(f"Error {response.status_code}: The server encountered an issue.")
            if response.status_code == 502:
                print("The server may be down or restarting. Please try again later.")
            elif response.status_code == 404:
                print("The requested resource was not found.")
            # Add other specific status code checks as needed

    except requests.exceptions.ConnectionError:
        print("Failed to connect to the server. Please check your connection.")
    except json.JSONDecodeError:
        print("Received non-JSON response from the server.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

print("Goodbye!")
