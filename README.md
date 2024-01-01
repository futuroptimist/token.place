# token.place
p2p generative AI marketplace

## roadmap

- [x] hello world: it literally just echoes your message param back to you
- [ ] server.py MVP: set up CI/CD, domain name, etc.
- [ ] host.py MVP: sign up as a host and manage your account
- [ ] host.py model management: browse available models, download, and manage downloads
- [ ] client.py MVP: access host compute via the server (batch output)
- [ ] client.py streaming output
- [ ] image support

## usage

1. start the script

```sh
python server.python
```

2. navigate to http://localhost:3000/?message=YOUR_MESSAGE_HERE

3. edit the message param and visit the page to see your message update