"""Safety check run before publishing: stops the run if the dashboard's code has a syntax error,
so a broken page is never published (the last working version stays online)."""
import re
import subprocess
import sys
import tempfile

html = open(sys.argv[1] if len(sys.argv) > 1 else "dashboard.html", encoding="utf-8").read()
scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
if not scripts:
    sys.exit("No script found in the dashboard page")
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
    f.write("\n;\n".join(scripts))
r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
if r.returncode:
    print(r.stderr)
    sys.exit("The dashboard page has a code error, so it was not published.")
print("Dashboard page code OK")
