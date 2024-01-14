import requests
import json
import re  # Import the 're' module for regular expressions

# URL of the server running locally
server_url = 'http://localhost:3000/'

# Initialize an empty chat history
chat_history = []

print("Welcome to the Chat Client!")
print("Type your messages and press Enter to send. Type 'exit' to quit.")

while True:
    # Get user input
    user_message = input("You: ")

    # Check if the user wants to exit
    if user_message.lower() == 'exit':
        break

    # Add the user message to the chat history
    chat_history.append({"role": "user", "content": user_message})

    # Prepare the JSON data to send to the server
    data = {
        "message": user_message,
        "chat_history": chat_history
    }

    try:
        # Send a POST request to the server
        response = requests.post(server_url, json=data)

        # Check if the request was successful
        if response.status_code == 200:
            # Parse the response JSON and display the assistant's reply
            response_data = response.json()
            for message in response_data:
                if message.get("role") == "assistant":
                    content = message.get("content")
                    if isinstance(content, dict) and "choices" in content:
                        # Extract the response from the 0th index of choices
                        response_text = content["choices"][0]["message"]["content"]

                        # Remove leading spaces and excessive new lines
                        cleaned_response = re.sub('\n+', '\n', response_text).strip()
                        print("AI:", cleaned_response)
                    else:
                        print("AI: [Unsupported response format]")
        else:
            print("Error:", response.text)

    except Exception as e:
        print("An error occurred:", str(e))

print("Goodbye!")
