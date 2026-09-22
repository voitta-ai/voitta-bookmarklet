# voitta-share — report sharing service

One Lambda behind an HTTP API; reports are served from S3 through CloudFront.
See the docstring in `handler.py` for the trust model. Provisioned by hand on
2026-09-22 in account 784666825494, us-west-2 (CloudFront/ACM in us-east-1).

| Resource | Name / id |
|---|---|
| Lambda | `voitta-share` (py3.12, arm64) — role `voitta-share-lambda` |
| HTTP API | `voitta-share`, id `072xwl2byi`, throttle 0.5 req/s burst 10 |
| Bucket | `voitta-shares` — Block Public Access on, SSE-S3, lifecycle by `ttl` tag |
| CloudFront | `E1X7YJDHCYDT9E`, OAC `E2WBLNYH5LSCEP`, alias `share.voitta.ai` |
| CF Function | `voitta-shares-index` (viewer-request): rewrites `/r/<id>/` → `/r/<id>/index.html`. S3's REST endpoint has no index document, so without this every share URL 404s. |
| Cert | existing `*.voitta.ai` (us-east-1) |
| Budget | `voitta-shares-guard`, $5/mo, alerts at 80% / 100% |

## The boundary
The Lambda role can `PutObject` / `DeleteObject` / `PutObjectTagging` on `r/*`
and **nothing else** — no `ListBucket`, no `GetObject`. CloudFront can read
`r/*` and `errors/*` and nothing else. No single identity can both write and
read. The error page lives outside `r/` so a leaked write key cannot replace it.

## Redeploy the function
    cd backend/share_service && zip -j /tmp/voitta-share.zip handler.py
    aws lambda update-function-code --region us-west-2 --function-name voitta-share --zip-file fileb:///tmp/voitta-share.zip

## Rotate the shared key
Generate a new `vsk_…`, then set `SHARE_KEY_HASH` (sha256 hex) in the Lambda
environment. Every install must then be given the new plaintext — one key,
one rotation. Old shares stay up; new uploads need the new key.
