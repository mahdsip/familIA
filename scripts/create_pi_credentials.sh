#!/usr/bin/env bash
# Creates an IAM access key for the Raspberry Pi client user and writes it to a
# local file that is NEVER committed (see .gitignore). Run once after apply.
#
# The user itself is created by Terraform (least privilege: invoke the query
# route only). Terraform does NOT create the secret key, so no secret ever
# lands in Terraform state or the repo.
#
# Usage:
#   AWS_PROFILE=<profile> ./scripts/create_pi_credentials.sh [user-name] [out-file]
set -euo pipefail

USER_NAME="${1:-familia-pi-client}"
OUT_FILE="${2:-pi-credentials.env}"

if [ -f "${OUT_FILE}" ]; then
  echo "Refusing to overwrite existing ${OUT_FILE}. Move it aside first." >&2
  exit 1
fi

echo "Creating access key for IAM user ${USER_NAME}..."
CREDS=$(aws iam create-access-key --user-name "${USER_NAME}" --output json)

AK=$(echo "${CREDS}" | python3 -c "import json,sys;print(json.load(sys.stdin)['AccessKey']['AccessKeyId'])")
SK=$(echo "${CREDS}" | python3 -c "import json,sys;print(json.load(sys.stdin)['AccessKey']['SecretAccessKey'])")

umask 077
cat > "${OUT_FILE}" <<EOF
# familIA Raspberry Pi client credentials — KEEP SECRET, do not commit.
export AWS_ACCESS_KEY_ID=${AK}
export AWS_SECRET_ACCESS_KEY=${SK}
export AWS_REGION=eu-central-1
EOF

echo "Wrote ${OUT_FILE} (chmod 600). Copy it to the Raspberry Pi over a secure channel (scp)."
echo "To rotate later: aws iam delete-access-key --user-name ${USER_NAME} --access-key-id <old>"
