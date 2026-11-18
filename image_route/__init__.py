"""The image route: an image-only design PDF, read page by page by the model on Bedrock, built into a transcription with
the transcribe-design-scan skill's own build script, and handed back to intake as an upload of its own.

    settings.py   the reader's environment variables, each with its default
    render.py     the skill's scripts, loaded from its folder; page images and crops, kept within the image limits
    prompt.py     the system prompt: a short framing, then the skill's reading rules, read from its folder
    converse.py   the Bedrock call: retries with backoff, the deadline, either API
    reader.py     the two tools, one page's conversation, the document, the accounting
    trial.py      python -m image_route.trial <pdf> --out <dir>: a document read for real, with its evidence
    handback.py   the two checks on a built transcription, its labels, and its put at intake
    task.py       python -m image_route.task: the Fargate task, a supervisor and the work it runs under a cap

A run that fails IMAGE_ONLY asks for its transcription, and lambda_function.py's starter and watcher start and watch
the task; the request, its row and its ending are defined there.
"""
