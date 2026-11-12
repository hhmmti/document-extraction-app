#!/bin/bash
# Builds the image from the app folder container/build.py wrote, for linux/amd64, and runs it on this machine:
#
#     container/run_local.sh <platform repo>/apps/enterprise/esp-design-extraction <AWS profile> [port]
#
# The container runs as the developer, not as the platform.  It gets the settings of the folder's app.config.json,
# less four:
#   ESP_CROSS_ACCOUNT_ROLE_ARN, ESP_CROSS_ACCOUNT_EXTERNAL_ID   on the platform the app assumes the data role; here
#                                it uses the profile's own credentials
#   ESP_SIGNED_IN_USER_HEADER    nothing in front of a local container signs anyone in, so who confirms is the
#                                profile's own session name
#   ESP_ATHENA_OUTPUT_LOCATION   the well lists use the app's default results location, not the platform's
# The AWS CLI reads the credentials from the profile into this shell's environment, and Docker passes them on by
# name: they are never written to a file or put on a command line.  They are temporary; when they expire, stop the
# container and run this again.
# Only this machine can reach the app (127.0.0.1).  Without a profile, nothing is built or run.
set -euo pipefail
if [ $# -lt 2 ] || [ -z "$2" ]; then
  echo "Refused: name the app folder and the AWS profile to take credentials from." >&2
  echo "usage: $0 <app folder> <AWS profile> [port]" >&2
  exit 2
fi
FOLDER=$1 PROFILE=$2 PORT=${3:-8501}
IMAGE=esp-design-extraction-app:local
[ -f "$FOLDER/app.config.json" ] || { echo "Refused: $FOLDER has no app.config.json; build it with container/build.py." >&2; exit 2; }

SETTINGS=()
while IFS= read -r setting; do SETTINGS+=(-e "$setting"); done < <(python3 -c '
import json, sys
for name, value in json.load(open(sys.argv[1]))["environment"].items():
    if name not in ("ESP_CROSS_ACCOUNT_ROLE_ARN", "ESP_CROSS_ACCOUNT_EXTERNAL_ID", "ESP_SIGNED_IN_USER_HEADER",
                    "ESP_ATHENA_OUTPUT_LOCATION"):
        print(f"{name}={value}")' "$FOLDER/app.config.json")
[ ${#SETTINGS[@]} -gt 0 ] || { echo "Refused: no settings read from $FOLDER/app.config.json." >&2; exit 2; }

CREDENTIALS=$(aws configure export-credentials --profile "$PROFILE" --format env) || {
  echo "Refused: no credentials from the profile $PROFILE. Sign in (aws sso login --profile $PROFILE) and run this again." >&2
  exit 1
}
eval "$CREDENTIALS"
unset CREDENTIALS

docker build --platform linux/amd64 -t "$IMAGE" "$FOLDER"
echo
echo "Running as the developer, not as the platform: with the profile $PROFILE's own credentials and no data role."
echo "Who confirms is that profile's session name; the well lists use the app's default results location."
echo "Open http://localhost:$PORT/enterprise/esp-design-extraction/   (Ctrl-C stops it)"
exec docker run --rm --platform linux/amd64 -p "127.0.0.1:$PORT:80" "${SETTINGS[@]}" \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN "$IMAGE"
