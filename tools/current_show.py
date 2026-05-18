#!/usr/bin/env python3
"""Return the active WIT Radio show based on local time."""

from datetime import datetime

hour = datetime.now().hour

if 6 <= hour < 9:
    show = "morning_acoustic"
elif 9 <= hour < 20:
    show = "day_shift"
elif 20 <= hour < 24:
    show = "dance_party"
else:
    show = "night_shift"

print(show)
