# WIT Radio Cloudflare Tunnel Setup

## Goal

Expose the local WIT Radio stream securely without opening router ports.

## Install Cloudflared

```bash
brew install cloudflared
```

## Authenticate

```bash
cloudflared tunnel login
```

## Create Tunnel

```bash
cloudflared tunnel create witradio
```

## Route DNS

```bash
cloudflared tunnel route dns witradio radio.weinsurethings.com
```

## Example Config

```yaml
tunnel: witradio
credentials-file: /Users/USERNAME/.cloudflared/credentials.json

ingress:
  - hostname: radio.weinsurethings.com
    service: http://localhost:8000
  - service: http_status:404
```

## Run Tunnel

```bash
cloudflared tunnel run witradio
```

## Recommended Public URLs

```text
https://radio.weinsurethings.com
https://radio.weinsurethings.com/witradio
```

## Benefits

- avoids port forwarding
- improves security
- easier SSL setup
- stable public access
- easy future scaling
