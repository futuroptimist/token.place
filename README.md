# token.place
p2p generative AI marketplace

# vision
There are tons of personal computers and homelabs out there with lots of compute that remain idle. This project aims to create a marketplace of people with spare compute and people with needs for compute. Note that this is not a financial marketplace -- this is intended to be a public good. If it takes off is anyone's guess, but I'll donate whatever compute I can in the meantime once this is up and running.

## roadmap

- [x] hello world: it literally just echoes your message param back to you
- [x] find an initial model to support (llama 2 7b chat gguf)
- [ ] download model programmatically on device
- [ ] load the model and successfully run it
- [ ] do inference over HTTP (including creation of a client.py)

## usage

1. start the script

```sh
python server.py
```

2. navigate to http://localhost:3000/?message=YOUR_MESSAGE_HERE

3. edit the message param and visit the page to see your message update