"""The image route's reader: an image-only design PDF, read page by page by the model on Bedrock and built into a
transcription with the transcribe-design-scan skill's own build script.

    settings.py   the environment variables, each with its default
    render.py     the skill's scripts, loaded from its folder; page images and crops, kept within the image limits
    prompt.py     the system prompt: a short framing, then the skill's reading rules, read from its folder
    converse.py   the Bedrock call: retries with backoff, the deadline, either API
    reader.py     the two tools, one page's conversation, the document, the accounting
    trial.py      python -m image_route.trial <pdf> --out <dir>: a document read for real, with its evidence

Nothing here is called by the pipeline yet.
"""
