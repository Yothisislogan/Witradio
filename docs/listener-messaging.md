# WIT Radio Listener Messaging

## Goal

Allow listeners to send short messages, song requests, shoutouts, and questions that can be turned into safe on-air AI responses.

## Recommended Message Types

- shoutout
- song request
- insurance question
- agent kudos
- funny comment
- station feedback

## Safety Rules

- do not read private personal information on air
- do not give binding insurance advice
- do not promise coverage or claim outcomes
- do not repeat offensive language
- do not mention full phone numbers or email addresses on air
- summarize risky messages instead of reading them verbatim

## Suggested Intake Fields

```json
{
  "name": "First name only",
  "city": "Optional",
  "message_type": "shoutout",
  "message": "Listener message",
  "permission_to_air": true
}
```

## AI Response Prompt

Turn this listener message into a short, friendly WIT Radio response under 20 seconds.
Keep it music-first, warm, and safe for broadcast.
If the message asks an insurance question, answer generally and invite the listener to contact We Insure Things for personalized help.

## Future Options

- website form
- SMS intake
- WhatsApp intake
- Apple Messages for Business
- internal moderation queue
- auto-generated Kokoro TTS response
