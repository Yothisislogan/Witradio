# WIT Radio Mac Mini Runbook

## Goal

Run WIT Radio locally first, then expose it publicly after audio is stable.

## Step 1: Clone the Repo

```bash
git clone https://github.com/Yothisislogan/Witradio.git
cd Witradio
```

## Step 2: Install Dependencies

```bash
chmod +x scripts/mac-mini-setup.sh
./scripts/mac-mini-setup.sh
```

## Step 3: Configure Icecast

Make sure the Icecast source password matches the password used by ezstream or the WRIT-FM runtime.

## Step 4: Start Icecast

```bash
icecast -c /opt/homebrew/etc/icecast.xml
```

Open:

```text
http://localhost:8000
```

## Step 5: Start the WRIT Runtime

```bash
./writ start icecast
./writ start stream
./writ status
```

## Step 6: Generate Content

```bash
./writ generate talk
./writ generate music
```

## Step 7: Test the Stream

Open:

```text
http://localhost:8000/witradio
```

## Step 8: Test the Web Player

Open:

```text
web/radio-player.html
```

## Step 9: Keep It Running

Use tmux-managed WRIT commands:

```bash
./writ start all
./writ logs stream -f
./writ status all
```

## Step 10: Public Deployment

Only expose the station publicly after local testing works for several hours without errors.
