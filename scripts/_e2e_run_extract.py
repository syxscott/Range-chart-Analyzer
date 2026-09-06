# E2E real-API test harness: run the project's auto-mode extraction on
# literature figures. Reads API credentials from a temp env file OUTSIDE the
# repo; the key is never printed or written into the repo.
import os, sys, json, base64, time
sys.stdout.reconfigure(encoding='utf-8')
# The user's machine runs a system proxy (FlClash on 127.0.0.1:7890) that
# refuses connections; the MiniMax endpoint is reachable directly (verified
# via curl). Bypass ALL proxies for this harness.
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

ENV_PATH = r"C:\Users\Administrator\AppData\Local\Temp\rca_e2e_api.env"
ENV = {}
with open(ENV_PATH, encoding="utf-8") as f:
    for line in f:
        if "=" in line:
            k, v = line.strip().split("=", 1)
            ENV[k] = v

from rca_core.extractor import extract, load_image_b64
from rca_core.llm import ApiFormat, LlmProvider

FIGDIR = os.path.join("outputs", "e2e_radiolaria")
RES = os.path.join(FIGDIR, "results")
os.makedirs(RES, exist_ok=True)

def run_one(item):
    name = item["image"]
    out_path = os.path.join(RES, name.replace(".png", ".json"))
    if os.path.isfile(out_path):
        return name, "cached"
    img_path = os.path.join(FIGDIR, name)
    try:
        b64, mime, w, h, resized, err = load_image_b64(img_path, max_edge=2400)
    except Exception as e:
        return name, f"load-error {e}"
    if err or not b64:
        return name, f"decode-error {err}"
    provider = LlmProvider(
        name="e2e-minimax",
        api_format=ApiFormat.ANTHROPIC,
        endpoint=ENV["ANTHROPIC_BASE_URL"],
        api_key=ENV["ANTHROPIC_API_KEY"],
        model=ENV["ANTHROPIC_MODEL"],
    )
    last = None
    for attempt in range(3):
        try:
            r = extract(mode="auto", image_b64=b64, media_type=mime,
                        caption=item["caption"], provider=provider,
                        timeout_sec=240)
        except Exception as e:
            last = {"ok": False, "error_key": "exception", "raw": str(e)[:300]}
            time.sleep(15 * (attempt + 1))
            continue
        if r.ok:
            last = {
                "ok": True, "mode_used": r.mode_used, "mode_source": r.mode_source,
                "truncated": r.truncated, "warning": r.warning,
                "latency_ms": r.latency_ms, "data": r.data,
                "raw": (r.raw or "")[:9000],
            }
            break
        # retry only transport-ish failures
        if r.error_key in ("err.http", "err.network", "err.timeout") or (r.status or 0) >= 500:
            last = {"ok": False, "error_key": r.error_key, "status": r.status,
                    "error_body": (r.error_body or "")[:300]}
            time.sleep(15 * (attempt + 1))
            continue
        last = {"ok": False, "error_key": r.error_key, "status": r.status,
                "error_body": (r.error_body or "")[:300]}
        break
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"manifest": item, "result": last}, f, ensure_ascii=False, indent=1)
    tag = "OK" if last and last.get("ok") else "FAIL"
    return name, f"{tag} mode={last.get('mode_used','') if last and last.get('ok') else last.get('error_key')}"

if __name__ == "__main__":
    from concurrent.futures import ThreadPoolExecutor
    only = sys.argv[1] if len(sys.argv) > 1 else None
    with open(os.path.join(FIGDIR, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    items = [it for it in manifest if not only or only in it["image"]]
    with ThreadPoolExecutor(max_workers=4) as ex:
        for name, status in ex.map(run_one, items):
            print(f"{name}  {status}", flush=True)
