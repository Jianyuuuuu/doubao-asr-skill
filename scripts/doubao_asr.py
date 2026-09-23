"""Doubao Seed-ASR 2.0 standard: resumable recognition and local timing exports."""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/"
RESOURCE = "volc.seedasr.auc"
SUCCESS = "20000000"
PENDING = {"20000001", "20000002"}


class UserError(Exception):
    """A deliberately non-sensitive user-facing error."""


def save_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def secret(name, prompt=False):
    value = os.environ.get(name, "").strip()
    if not value and prompt and sys.stdin.isatty():
        value = getpass.getpass(name + " (hidden): ").strip()
    if not value:
        raise UserError("Missing " + name + "; configure it locally or use --prompt in a terminal.")
    return value


def auth_headers(mode, prompt=False):
    if mode == "api-key":
        return {"X-Api-Key": secret("DOUBAO_API_KEY", prompt)}
    return {"X-Api-App-Key": secret("DOUBAO_APP_ID", prompt),
            "X-Api-Access-Key": secret("DOUBAO_ACCESS_TOKEN", prompt)}


def validate_url(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise UserError("Audio must have an HTTPS URL without embedded username/password.")
    if parsed.fragment:
        raise UserError("Audio URL must not contain a fragment.")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Do not forward authentication headers to a redirect target.


def api_call(action, auth, task_id, body, timeout=30):
    if action not in ("submit", "query"):
        raise UserError("Unsupported API action.")
    headers = {**auth, "Content-Type": "application/json", "X-Api-Resource-Id": RESOURCE,
               "X-Api-Request-Id": task_id, "X-Api-Sequence": "-1"}
    request = urllib.request.Request(BASE + action,
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=headers, method="POST")
    try:
        response = urllib.request.build_opener(NoRedirect).open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        http_code = response.code
        code = response.headers.get("X-Api-Status-Code", "")
        # Never print/store provider messages, headers, URL, or exception text.
        if not code.isdigit() or len(code) > 12:
            code = "unknown"
        payload = {}
        if action == "query" and 200 <= http_code < 300 and code == SUCCESS:
            payload = json.loads(response.read())
        return http_code, code, payload


def request_body(url, audio_format, hotwords):
    body = {"audio": {"url": validate_url(url), "format": audio_format},
            "request": {"model_name": "bigmodel", "enable_itn": False,
                        "enable_punc": True, "enable_ddc": False,
                        "show_utterances": True, "enable_speaker_info": False}}
    if hotwords:
        body["request"]["corpus"] = {"context": json.dumps(
            {"hotwords": [{"word": word} for word in hotwords]}, ensure_ascii=False)}
    return body


def timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value != value or value == float("inf"):
        return None
    return value


def clean_response(value, secrets=()):
    """Keep recognition content while excluding echoed transport credentials/URLs."""
    forbidden = {"url", "audio_url", "download_url", "inline_url", "authorization", "headers",
                 "token", "access_token", "api_key", "secret_key", "x-api-key", "x-api-access-key"}
    if isinstance(value, dict):
        return {key: clean_response(item, secrets) for key, item in value.items()
                if key.lower() not in forbidden}
    if isinstance(value, list):
        return [clean_response(item, secrets) for item in value]
    if isinstance(value, str):
        for secret_value in secrets:
            if secret_value:
                value = value.replace(secret_value, "[REDACTED]")
    return value


def recognition_response(payload, secrets=()):
    """Allowlist recognition fields instead of archiving arbitrary provider echoes."""
    result = payload.get("result", {})
    if not isinstance(result, dict):
        raise UserError("Expected one result object; channel-split responses are not supported.")
    utterances = []
    for item in result.get("utterances", []):
        utterance = {key: item[key] for key in ("text", "start_time", "end_time") if key in item}
        utterance["words"] = [{key: word[key] for key in ("text", "start_time", "end_time", "confidence") if key in word}
                              for word in item.get("words", [])]
        utterances.append(utterance)
    retained = {"result": {"text": result.get("text", ""), "utterances": utterances}}
    audio_info = payload.get("audio_info", {})
    if isinstance(audio_info, dict) and "duration" in audio_info:
        retained["audio_info"] = {"duration": audio_info["duration"]}
    return clean_response(retained, secrets)


def normalize(payload, outdir, gap_ms=200):
    """Retain actual model timing; never fabricate word or character alignment."""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    result = payload.get("result", {})
    if not isinstance(result, dict):
        raise UserError("Expected one result object; channel-split responses are not supported.")
    utterances = result.get("utterances", [])
    if not isinstance(utterances, list):
        raise UserError("Invalid utterance structure.")
    words, invalid = [], 0
    for index, utterance in enumerate(utterances):
        for word in utterance.get("words", []):
            start, end = timestamp(word.get("start_time")), timestamp(word.get("end_time"))
            if start is None or end is None or end < start:
                invalid += 1
                continue
            words.append({"utterance": index, "text": word.get("text", ""),
                          "start_time": start, "end_time": end})
    words.sort(key=lambda w: (w["start_time"], w["end_time"]))
    gaps = []
    previous = None
    # Track the furthest covered endpoint; overlapping tokens are not silent gaps.
    for word in words:
        if previous is not None and word["start_time"] - previous["end_time"] >= gap_ms:
            gaps.append({"start_ms": previous["end_time"], "end_ms": word["start_time"],
                         "duration_ms": word["start_time"] - previous["end_time"],
                         "kind": "between_utterances" if previous["utterance"] != word["utterance"] else "within_utterance",
                         "left_text": previous["text"], "right_text": word["text"]})
        if previous is None or word["end_time"] > previous["end_time"]:
            previous = word
    coverage = "word" if words else "utterance" if utterances else "text_only"
    report = {"timestamp_unit": "ms", "granularity": coverage, "invalid_words_skipped": invalid,
              "utterances": utterances, "words": words, "gap_candidates": gaps,
              "text": result.get("text", ""), "gap_threshold_ms": gap_ms,
              "note": "Timing gaps are candidates, not verified silence or automatic edit decisions."}
    save_json(out / "transcript.json", report)
    save_json(out / "words-ms.json", {"unit": "ms", "granularity": coverage, "words": words})
    rows = []
    for utterance in utterances:
        start, end = timestamp(utterance.get("start_time")), timestamp(utterance.get("end_time"))
        times = f"{start / 1000:.3f} - {end / 1000:.3f}" if start is not None and end is not None else "timing unavailable"
        rows.append(f"[{times}] {utterance.get('text', '')}")
    (out / "transcript.txt").write_text("\n".join(rows) if rows else result.get("text", ""), encoding="utf-8")
    with (out / "gap-candidates.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["start_ms", "end_ms", "duration_ms", "kind", "left_text", "right_text"])
        writer.writeheader()
        # Guard spreadsheet formula interpretation in recognised text.
        for gap in gaps:
            row = dict(gap)
            for key in ("left_text", "right_text"):
                text = str(row[key])
                row[key] = "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text
            writer.writerow(row)
    return {"utterances": len(utterances), "words": len(words), "gap_candidates": len(gaps), "granularity": coverage}


def run(args, call=api_call, sleep=time.sleep):
    audio, out = Path(args.audio), Path(args.outdir)
    if not audio.is_file():
        raise UserError("Local audio file does not exist.")
    if audio.stat().st_size > 512 * 1024 * 1024:
        raise UserError("Audio exceeds the documented 512 MB limit.")
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "task.json"
    digest = sha256(audio)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if state and (state.get("source_sha256") != digest or state.get("resource_id") != RESOURCE):
        raise UserError("Audio/model differs from saved task; use a separate output directory.")
    if state.get("status") == "complete":
        raw_path = out / "response.json"
        if not raw_path.exists():
            raise UserError("Completed task is missing response.json; restore it or query the saved task manually.")
        normalize(json.loads(raw_path.read_text(encoding="utf-8")), out, args.gap_ms)
        print("Reused completed response; no network request.")
        return 0
    if state.get("status") == "submit_rejected":
        raise UserError("Saved task failed; inspect task.json codes and the official documentation before retrying.")
    auth = auth_headers(args.auth, args.prompt)
    redactions = [*auth.values(), os.environ.get("DOUBAO_AUDIO_URL", "")]
    if not state.get("task_id"):
        hotwords = []
        if args.hotwords:
            hotwords = [line.strip() for line in Path(args.hotwords).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        url = secret("DOUBAO_AUDIO_URL", args.prompt)
        redactions.append(url)
        body = request_body(url, args.format, hotwords)
        state = {"source_sha256": digest, "resource_id": RESOURCE, "timestamp_unit": "ms",
                 "task_id": str(uuid.uuid4()), "status": "submitting", "submitted_at": time.time()}
        save_json(state_path, state)  # Persist ID BEFORE request: an ambiguous submit must never be repeated automatically.
        try:
            http, code, _ = call("submit", auth, state["task_id"], body)
        except Exception:
            state["status"] = "submit_unknown"
            save_json(state_path, state)
            raise UserError("Submission outcome unknown. Rerun the same command to query the saved task, not submit again.") from None
        finally:
            body["audio"]["url"] = None
            url = None
        state.update({"http_status": http, "api_code": code})
        if not 200 <= http < 300 or code != SUCCESS:
            state["status"] = "submit_unknown" if http >= 500 or code == "unknown" else "submit_rejected"
            save_json(state_path, state)
            if state["status"] == "submit_unknown":
                raise UserError("Submission outcome unknown. Rerun to query the saved task without resubmitting.")
            raise UserError("Submit rejected; inspect numeric HTTP/API codes in task.json.")
        state["status"] = "submitted"
        save_json(state_path, state)
    deadline = time.monotonic() + args.wait_seconds
    while True:
        try:
            http, code, payload = call("query", auth, state["task_id"], {})
        except Exception:
            state["status"] = "query_interrupted"
            save_json(state_path, state)
            raise UserError("Query interrupted. Rerun the same command to resume the saved task.") from None
        state.update({"http_status": http, "api_code": code, "last_checked_at": time.time()})
        if 200 <= http < 300 and code == SUCCESS:
            payload = recognition_response(payload, redactions)
            save_json(out / "response.json", payload)
            summary = normalize(payload, out, args.gap_ms)
            state.update({"status": "complete", **summary})
            save_json(state_path, state)
            print(json.dumps({"status": "complete", **summary}))
            return 0
        if http == 429 or http >= 500:
            state["status"] = "query_interrupted"
            save_json(state_path, state)
            raise UserError("Query temporarily unavailable. Resume later with the same output directory.")
        if not 200 <= http < 300 or code not in PENDING:
            state["status"] = "query_failed"
            save_json(state_path, state)
            raise UserError("Query failed; inspect numeric HTTP/API codes in task.json.")
        state["status"] = "processing"
        save_json(state_path, state)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print("Task pending. Rerun with the same audio and output directory to resume.")
            return 2
        sleep(min(args.poll_seconds, remaining))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    transcribe = subs.add_parser("transcribe", help="Submit/resume a standard recognition task")
    transcribe.add_argument("--audio", required=True, help="Local copy of exactly the same audio served by DOUBAO_AUDIO_URL")
    transcribe.add_argument("--outdir", required=True)
    transcribe.add_argument("--format", choices=("wav", "mp3", "ogg", "spx", "amr", "aac", "m4a"), default="wav")
    transcribe.add_argument("--auth", choices=("api-key", "legacy"), default="api-key")
    transcribe.add_argument("--prompt", action="store_true", help="Prompt for missing values in a real terminal; never echo them")
    transcribe.add_argument("--hotwords", help="Optional UTF-8 text file, one term per line")
    transcribe.add_argument("--wait-seconds", type=float, default=0, help="Local polling budget; zero performs one query")
    transcribe.add_argument("--poll-seconds", type=float, default=20)
    normal = subs.add_parser("normalize", help="Export timing and gap candidates from a saved response, without network")
    normal.add_argument("--input", required=True)
    normal.add_argument("--outdir", required=True)
    for command in (transcribe, normal):
        command.add_argument("--gap-ms", type=float, default=200)
    args = parser.parse_args()
    if args.gap_ms < 0 or not args.gap_ms < float("inf"):
        raise UserError("--gap-ms must be finite and nonnegative.")
    if args.command == "normalize":
        summary = normalize(json.loads(Path(args.input).read_text(encoding="utf-8")), args.outdir, args.gap_ms)
        print(json.dumps(summary))
        return 0
    if not 0 <= args.wait_seconds <= 10800 or not 1 <= args.poll_seconds <= 60:
        raise UserError("Use --wait-seconds 0..10800 and --poll-seconds 1..60.")
    return run(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except UserError as exc:
        print("Error: " + str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        # Exception messages may contain URLs/paths/response bodies; report only type.
        print("Error: " + type(exc).__name__ + "; inspect local inputs without sharing credentials.", file=sys.stderr)
        sys.exit(1)
