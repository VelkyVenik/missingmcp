# Mail campaigns

One plain-text file per campaign, `<slug>.txt`:

```
Subject: The subject line

Body text, plain. Write it like a personal note.
```

The unsubscribe footer is appended automatically. A campaign's text is
snapshotted into the DB at `create`, so editing the file afterwards changes
nothing already created. Flow and commands: `scripts/campaign.py` and
README → Monitoring.
