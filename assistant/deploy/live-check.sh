#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
WHAT="${1:-all}"
SSH="${FA_SSH:?set FA_SSH to the ssh command that reaches the VM, e.g. FA_SSH=\"ssh -p 22 deploy@vm.example\"}"
FX="$(mktemp -d)"; trap 'rm -rf "$FX"' EXIT

say -v Samantha -o "$FX/voice.aiff" "Hi. A client writes that the Next button is not visible on the course page after the third lesson. Please take a look."
ffmpeg -loglevel error -y -i "$FX/voice.aiff" -c:a libopus "$FX/voice.ogg"
say -v Samantha -o "$FX/nightshift-voice.aiff" "There is no picture, the banner is text only, in orange. Text: webinar, artificial intelligence in the classroom, November twentieth at four p.m. Show it to all learners up to and including November twentieth, then the banner goes away by itself. No need to approve it with Dana."
ffmpeg -loglevel error -y -i "$FX/nightshift-voice.aiff" -c:a libopus "$FX/nightshift-voice.ogg"
.venv/bin/python - "$FX" <<'EOF'
import sys, email.message, docx, openpyxl
from PIL import Image, ImageDraw, ImageFont
fx = sys.argv[1]
img = Image.new("RGB", (900, 260), "white"); draw = ImageDraw.Draw(img)
draw.text((40, 100), "Error 502 — payment page", fill="black",
          font=ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 46))
img.save(f"{fx}/shot.png")
d = docx.Document(); d.add_paragraph("Brief for the factory"); d.add_paragraph("Code word: GIRAFFE-17"); d.save(f"{fx}/brief.docx")
wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Grades"
for row in (["email", "grade"], ["taras@school.ua", 64], ["olena@school.ua", 87]): ws.append(row)
wb.save(f"{fx}/grades.xlsx")
m = email.message.EmailMessage(); m["From"] = "client@school.ua"; m["Subject"] = "The certificate does not arrive after the course"
m.set_content("Hello! The learners finished the course, but the certificate does not arrive."); open(f"{fx}/mail.eml", "wb").write(bytes(m))
EOF
REMOTE="/tmp/fa-fixtures-$$"
tar -C "$FX" -cf - voice.ogg nightshift-voice.ogg shot.png brief.docx grades.xlsx mail.eml | $SSH "mkdir -p $REMOTE && tar -C $REMOTE -xf -"
$SSH "LIVE_TICKET=${LIVE_TICKET:-} LIVE_EDIT_TICKET=${LIVE_EDIT_TICKET:-} ASSISTANT_PORT=${ASSISTANT_PORT:-8190} /opt/arceus-assistant/venv/bin/python /opt/arceus-assistant/src/deploy/live-check.py $REMOTE $WHAT; rc=\$?; rm -rf $REMOTE; exit \$rc"
