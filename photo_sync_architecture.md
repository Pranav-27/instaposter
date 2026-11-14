# Photo Sync --- Architecture

## 1. Requirements

-   Capture approximately 5--10 photos per week (about 20--42 per month;
    HEIC iPhone originals). Only photos taken from the PhotoSync
    install date onward are ingested; the existing library is never
    processed.
-   Posting cadence: no fixed cap. Every collection that meets the
    readiness criteria (§5.4) is proposed.
-   Notify only when a collection is ready --- never a nightly digest.
-   Monthly themes: "October dump", trips (camping with Big Sur), events
    (night out in NYC).
-   People, trends, scenery, and details can be mixed in one post.
-   Temporary working copy: Google Photos remains the permanent backup.
-   Originals are never modified; every Instagram render is a new
    derivative from the original.
-   The home area (the configured home zones) is never named in
    captions or location tags.
-   The Instagram account is new (no post history); posts carry a
    location tag. Stories are a later feature.

## 2. Key external constraints

### Google Photos

-   Google Photos cannot feed the pipeline directly. Since 2025-03-31,
    the Library API only returns media created by the calling app; the
    library read scopes were removed and the Picker API requires manual
    selection.
-   Photos therefore reach S3 directly from the iPhone.

### Instagram Content Publishing API

-   JPEG only (no HEIC); sRGB; maximum 8 MB; width 320--1440 px;
    supported aspect ratio 4:5 to 1.91:1.
-   Carousel: up to 20 items; every item is cropped to the first item's
    aspect ratio (default 1:1).
-   Rendered media must be publicly reachable when Meta fetches it.
-   Requires a professional Creator or Business account. Stories are
    supported per image; publishing requires the Instagram Pages-related
    API permissions.
-   Not available via API: music, stickers, shopping tags.

### iPhone photo dimensions

-   iPhone portrait photos are 3:4, outside the allowed range; portrait
    shots lose approximately 6% of height when adapted to 4:5.

## 3. Design principles

1.  **Cadence by data, not by clock.** A daily check runs silently.
    Telegram is only messaged when a collection is ready and Claude
    judges it post-worthy.
2.  **Code for facts, Claude for judgment.** Time/GPS clustering,
    de-duplication, sharpness, and readiness are deterministic. Claude
    does tagging, selection, ordering, cropping, and captions.
3.  **Originals are immutable.** All analysis is derivative; the
    original stays a single-generation source.
4.  **What you approve is what gets posted.** Renders are produced at
    proposal time. The Telegram preview and Instagram upload are the
    same files.
5.  Each photo is posted at most once.
6.  **Fewest moving parts.** At this volume: no queues, no Step
    Functions, no vector database, and no batch inference.

## 4. System overview

1.  iPhone Photos → Google Photos (permanent backup; not used by the
    pipeline).
2.  PhotoSync, when charging → S3 originals (HEIC, untouched; expire
    after 365 days).
3.  S3 ObjectCreated event (image extensions only).
4.  Indexer → EXIF/GPS + derivatives + perceptual hash/sharpness;
    reverse geocode + Claude tagging.
5.  DynamoDB stores photos, collections, proposals, posts, and feedback.
6.  EventBridge Scheduler (daily) → evaluator builds collections and
    checks readiness; every ready collection is sent.
7.  Curator → Claude Opus selection/order/crops/captions.
8.  Renderer creates final image files.
9.  Telegram: album preview + caption options + buttons.
10. Telegram bot (Lambda Function URL) → approve / edit / reorder /
    reject.
11. Publisher → post now, or EventBridge Scheduler one-time schedule.

## 5. Components

### 5.1 Ingest --- iPhone → S3

-   **App:** PhotoSync Premium (automatic transfer to the S3 target; use
    proven features).
-   **Trigger:** When Charging (overnights; optionally location-aware,
    e.g. home).
-   **Format:** Original HEIC conversion off; metadata preserved. Use
    PhotoSync's "all images" filter.
-   **File naming:** Recording Time + Name (PhotoSync naming option).
    Files land under `originals/`. Names are for readability only; the
    indexer takes time from EXIF and identity from the SHA-256, not from
    the filename.
-   **Exclude:** PhotoSync does not send Live Photos (neither the still
    nor the MOV) or screenshots (the Screenshots album), and excludes any
    other albums the app allows excluding.
-   **Scope:** still images (HEIC/JPEG) taken on or after the install
    date; videos and RAW are also ignored. The indexer
    also drops anything with `DateTimeOriginal` before
    `INGEST_START_DATE` (§11).
-   **Credentials:** Dedicated IAM user for PhotoSync uploader with
    `s3:PutObject` on `originals/*`, plus `s3:ListBucket` and
    `s3:GetBucketLocation` on the bucket (connection test). PhotoSync
    requires upload permission, not read or delete.
-   Google Photos keeps backing up independently in its own app.

#### Observed first upload (2026-11-13)

-   **Key:** `originals/<device name>/Recents/<YYYY-MM-DD_HH-MM-SS>_IMG_<nnnn>.HEIC`.
    The device name contains spaces and a curly apostrophe (S3 events
    URL-encode keys; decode before use). PhotoSync also wrote a zero-byte
    `originals/` marker object.
-   **Object:** content type `image/heic`, uppercase `.HEIC` extension,
    SSE-S3, versioned, user metadata `mtime` (epoch seconds) set by
    PhotoSync. About 1.3 MB.
-   **Image:** HEIF, 8-bit RGB, embedded ICC profile (536 bytes), plus an
    auxiliary depth map (ignore). The pillow-heif decode is already
    rotated (2316×3088 portrait) while EXIF says 3088×2316 with
    `Orientation` 1, so do not apply a second rotation.
-   **EXIF preserved:** make/model, `DateTimeOriginal` (local time),
    `OffsetTimeOriginal` (`-05:00`), and GPS (latitude/longitude as
    degree-minute-second rationals, altitude, speed, direction, UTC
    time). GPS survives PhotoSync with conversion off.
-   **Processing notes:** the EventBridge suffix filter is case-sensitive,
    so match both `.heic` and `.HEIC`; ignore zero-byte keys; convert GPS
    to decimal degrees; the same photo can be uploaded under several
    album folders, which the SHA-256 de-duplication handles.

### 5.2 Storage --- S3

One private bucket: Block Public Access on; SSE-S3; versioning on
(guards against overwritten originals); lifecycle rules also expire
noncurrent versions after 30 days so expiry frees space.

  --------------------------------------------------------------------------
  Prefix                     Contents                Retention
  -------------------------- ----------------------- -----------------------
  `originals/`               HEIC at shot; PhotoSync Expire after 365 days
                             upload                  

  `derived/`                 JPEG, Claude views,     Expire after 365 days
                             metadata derivatives    

  `renders/<proposal_id>/`   Final, publish-ready    Expire after 30 days
                             JPEGs                   
  --------------------------------------------------------------------------

Storage cost is negligible (approximately 2--5 MB per photo; around 1--2
GB/year; not everything stays in S3 Standard).

### 5.3 Indexer (per photo)

**Trigger:** S3 → EventBridge rule (`originals/` prefix, image extensions only) → Lambda;
only two retries, on failure destination → SQS DLQ.

1.  SHA-256 of the object; skip if `PHOTO#<hash>` already exists (same
    photo uploaded twice).
2.  Decode with Pillow/HEIF: read EXIF `DateTimeOriginal`, GPS
    (latitude, longitude, altitude, speed, direction), and `LensModel`;
    extract the auxiliary depth map; set `is_selfie` when the lens is
    the front camera; camera timestamp overrides upload time; times are
    converted to America/New_York.
3.  Write derivatives (1024 px and 512 px JPEG; EXIF orientation
    applied).
4.  Perceptual hash (imagehash) and sharpness score; persist; detect
    near-duplicates and blur.
5.  Reverse geocode GPS with Amazon Location Service → neighborhood,
    city, region, country; flag `in_home_zone` (§5.4). Photos without
    GPS get no location here; the evaluator infers it.
6.  Claude Haiku 4.5 tagging on the 1024 px derivative. Structured
    output fields include:
    -   `tags` (e.g. people, scenery, food, nightlife, nature)
    -   `scene`
    -   `mood`
    -   `quality`
    -   `people`
    -   `food`
    -   `landmark`
    -   `is_photo`, `is_screenshot`, `is_document`, `is_receipt`
    -   `faces_visible`
    -   `eyes_closed`
    -   `blur_score` / quality score
    -   `text_visible`
    -   `notes`

    Screenshots, documents, receipts, and other non-photo images should
    be excluded from candidate collections.

### 5.4 Collections and readiness --- when to notify

A collection is a candidate post: a group of available photos that
belong together. Collections are recomputed from scratch by the daily
evaluator (not incrementally). Readiness counts postable photos after
near-duplicate collapse. Every postable photo belongs to exactly one
collection.

-   **Home zone:** the circles in `HOME_ZONES` (§11). A
    photo is "outside home" when its GPS is outside the home zone.
-   **GPS inference:** a photo without GPS takes the location of the
    nearest-in-time GPS photo within 12 hours. If there is none, or the
    photos before and after disagree on inside/outside home, the
    location is unknown and the photo can only join the monthly dump.
    Inference runs in the evaluator, because uploads arrive in batches.
-   **Time:** all times are America/New_York.

| Type | Membership | Minimum | Ready when |
|------|------------|---------|------------|
| Event | Consecutive photos inside the home zone with gaps of at most 3 hours, within 5 km of the group centroid | 3 | 24 hours after the last photo |
| Trip | Contiguous run of photos outside the home zone | 3 | First photo back inside the home zone, or 24 hours after the last photo |
| Monthly dump | Remaining postable photos not in an event or trip, from closed months | 10 | First evaluator run after the calendar month ends |

Rules:

-   No priority between types and no posting cap: every ready collection
    is proposed, concurrently. Membership is disjoint, so proposals
    never compete for photos.
-   Photos in a `SENT`, `APPROVED`, or `SCHEDULED` proposal are locked
    until it resolves.
-   Rejected or expired collections are suppressed until their
    membership changes; their photos stay in the pool. There is no
    cooldown.
-   Photos the curator does not select return to the pool and can feed
    the next monthly dump.
-   Screenshots, documents, receipts, and other non-photos never join a
    collection.

### 5.5 Curation

Claude Opus selects and orders the photos, proposes crops, and writes
captions.

Selection and ordering principles: - Prefer all photos for trips and
events; mix scenery, people, and details. - Slide 1 is the strongest,
most eye-catching image (cover, grid thumbnail, and first carousel
image). The carousel's aspect ratio is set by slide 1. - Narratives are
storytelling: details → people → closer/stronger image, or chronological
order when appropriate. - Avoid too many wide and close framings or two
nearly identical frames; keep color flow. - Captions should use places
and city/neighborhood level only, never street addresses. Locations
inside the home zone are never named. - Captions are English with 3
hashtags and are generated with the liked/disliked examples (§5.9). - Every slide is
cropped to 4:5.

Failure handling: if Claude fails, retry or wait; do not mark the
collection as completed. Alert after repeated failures.

### 5.6 Render

A library module used by the curator; one pass from the original per
photo:

1.  Decode HEIC; apply EXIF orientation.
2.  Crop at the AI-selected normalized box, mapped to the original
    photo.
3.  Resize to 1440 px maximum (Instagram maximum; do not let Instagram
    resample again).
4.  Convert Display P3 → sRGB from the embedded ICC profile.
5.  Encode JPEG, quality 95, 4:4:4 chroma; sRGB profile embedded; strip
    EXIF/GPS originals.

HDR gain maps are not carried over (SDR output).

### 5.7 Approval --- Telegram bot

-   Send the rendered files in order as photos. Preview compression is
    acceptable because the approved render is the file that is posted.
    Carousels over 10 images are sent as several albums with continuous
    numbering in the captions (e.g. 11--14 of 14).
-   Control message contains caption options and actions:
    -   **Caption chosen** --- select a suggested caption.
    -   **Edit caption** --- force-reply; the reply text becomes the
        caption.
    -   **Reorder / Remove** --- reply with directions (e.g. `3,1,2`) or
        remove an image; resend a new preview, version +1.
    -   **Regenerate** --- any free-text reply is feedback and produces 3 new caption options (§5.9).
    -   **Reject** --- reason buttons (photos / grouping / captions /
        not interesting); photos are released back to the pool and the collection is
        suppressed until its membership changes (§5.4).
-   Handler: Lambda Function URL. Verify the Telegram bot secret-token
    header and allow only the owner's chat ID.
-   Render immediately; heavy work is invoked asynchronously.
-   Idempotency: every version is a DynamoDB conditional write; only one
    version can be published.
-   Expiry: unapproved proposals expire after 7 days; photos return to
    the pool and the collection is suppressed until its membership
    changes.
-   Cost alerts: the bot Lambda is subscribed to the existing SNS topic
    `photo-sync-cost-alerts` (Cost Anomaly Detection, immediate, impact
    of $5 or more) and forwards each message to the owner's chat. The
    AWS Budgets alerts ($10/month: 50% actual, 100% actual, 100%
    forecasted) are added to the same topic. Email subscribers on the
    topic stay as a backup.

### 5.8 Publisher --- Instagram

Instagram API with Instagram Login; no Facebook Page-scoped Graph API
setup. Meta's app is in development mode with the owner's Creator
account.

1.  Consume the approved proposal's renders and caption.
2.  Generate presigned S3 URLs for each render (image URL, alt text,
    carousel container as applicable).
3.  Create the carousel container (children containers for each image);
    set caption on the parent container.
4.  Poll until the container is `FINISHED`, then publish.
5.  Save media ID and permalink; mark photos as posted and update
    cadence.
6.  For scheduled posts, EventBridge Scheduler invokes the publisher at
    the scheduled time.

Location tag: each post carries one tag, the most-represented place
outside the home zone; no tag if every photo is inside it. Instagram's
`location_id` is a Facebook Place ID, and whether the Instagram Login
API can look one up is unverified; it is tested in Phase 0. Fallback:
name the place in the caption.

The long-lived token is valid for 60 days. A weekly token refresher
checks expiry and refreshes when needed; alert if the token is older
than 30 days or refresh fails.

### 5.9 Conversational captions and learning

**Conversation.** Any free-text reply to a proposal is feedback. One
Claude call per message receives the proposal, its caption history, and
the message, and calls a tool: `generate_captions(instruction)`,
`select(n)`, `edit_text`, `reorder`, `remove_photo`, or `reject(reason)`.
Buttons remain for the fixed actions. Each caption option is its own
message, so a reply-to ("shorter") identifies the target. Each round
produces 3 options, generated with the whole history of this proposal
(earlier options and instructions) so they differ from what was already
shown. There is no limit on rounds. The bot acknowledges Telegram
immediately and does the work asynchronously.

**Learning (examples only).** Every caption generation receives the last
10 liked and the last 10 disliked captions, each tagged with its post
type (trip, event, monthly dump). Disliked examples include your
critique when you gave one. There is no style guide and no fine-tuning;
the example pools are the only learned state.

-   Liked: a published caption, including your manual edits.
-   Disliked: a candidate you answered with a change request or
    critique, or any candidate of a rejected proposal. Options you did
    not pick in the round you approved from are neutral.

**Approval.** After a proposal is published or rejected, the bot sends a
summary grouped by post type: the caption you picked, the candidates you
criticized with Claude's reading of why (e.g. "too plain; wants
funnier"), and the examples that would be added. Examples enter the
pools only after you approve; declining discards that proposal's
feedback.

**Hard rules.** Hashtag count, no home-zone place names, and no street
addresses are checked in code after each generation; failing candidates
are regenerated.

**Data.** `CAPTION#<proposal>#<n>` items hold the text, round, post
type, outcome (`SHOWN`, `PICKED`, `EDITED`, `CRITIQUED`, `REJECTED`),
critique, final text if edited, and an `approved_for_learning` flag. A
GSI on polarity and time returns the latest 10 approved examples of each
kind.

## 6. Data model --- DynamoDB (single table, on-demand)

Single-table design with partition/sort keys and GSIs for the main
access patterns.

  -----------------------------------------------------------------------
  Entity                              Key / purpose
  ----------------------------------- -----------------------------------
  Photo                               `PHOTO#<hash>`; metadata, tags,
                                      EXIF/GPS, sharpness, state, and
                                      whether posted

  Collection                          Collection ID/version; membership,
                                      time/location bounds, readiness,
                                      and status

  Proposal                            Proposal ID/version; chosen photos,
                                      order, crop, renders, captions, and
                                      state

  Post                                Post/media ID; caption, publish
                                      time, Instagram permalink, and
                                      status

  Feedback                            Proposal/version/action; reason,
                                      selected caption, edits, and
                                      reorder history
  -----------------------------------------------------------------------

Stored per Photo item:

-   **Identity and state:** SHA-256, S3 key, state, posted flag.
-   **Time:** capture time in America/New_York, original UTC offset.
-   **Location:** latitude, longitude, altitude, speed, direction,
    `in_home_zone`, place names (neighborhood, city, region, country).
-   **Camera:** lens model, and `is_selfie` (deterministic: front
    camera lens; separate from Claude's tags).
-   **Depth map:** stored as an S3 object under `derived/` with a
    pointer on the item (about 370×490; DynamoDB items cap at 400 KB).
-   **Quality:** perceptual hash, sharpness, and the Claude tag fields.
-   **Not stored:** the raw EXIF dump, which is re-read from the
    original while it exists (365 days).

Useful access patterns: - Photos by status and time. - Collections by
status and time. - Proposals by status. - Recent posts (for caption voice).

Proposal states: `SENT` → `APPROVED` → optionally `SCHEDULED` →
`PUBLISHING` → `PUBLISHED`; failure leads to `FAILED` with alert and
retry action. Rejected, expired, or removed photos return to the
available pool.

### 6.1 Caption cold start

The Instagram account is new, so there is no history to import and no
examples at first. Optionally seed the liked pool with 3--5 sample
captions. Reposting is prevented by Photo state alone.

## 7. Claude usage

  -----------------------------------------------------------------------
  Model                      Effort        Calls/month          Estimated
                                                               cost/month
  -------------- ------------------ ------------------ ------------------
  Claude Haiku                  ---               \~40   less than \$0.06
  4.5                                                  

  Curation +                 Medium             \~2--5  estimate shown as
  revisions:                                                a few dollars
  Claude Opus                                          
  -----------------------------------------------------------------------

-   Access Claude through AWS (Bedrock); use the AWS SDK with IAM
    authentication.
-   Keep model IDs configurable; use structured outputs, adaptive
    thinking, and low token budgets.
-   Starting models (us-east-1, verified on the account): tagging uses
    Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`); curation
    and captions use Opus 4.6 (`us.anthropic.claude-opus-4-6-v1`).
    Haiku 5.5 and Opus 4.7 and later returned "not available for this
    account"; revisit once access is granted.
-   Haiku returns JSON inside a markdown code fence unless constrained;
    use a tool schema or structured output, or strip the fence.
-   Estimates use approximate prices (Opus around \$5/\$25 per million
    input/output tokens; Haiku around \$0.10/\$0.50 per million tokens)
    and roughly one image tokenization per 1024 px image.
-   Batch processing is not used: at approximately 40 photos/month, the
    savings are cents and it adds latency.

## 8. AWS resources

Region: us-east-1 for all resources.

  -----------------------------------------------------------------------
  Resource                            Purpose
  ----------------------------------- -----------------------------------
  S3 bucket                           Originals, derivatives, renders

  DynamoDB table + GSI                Metadata and state

  Lambda ×6                           One ARM64 container image; separate
                                      handlers for indexer, evaluator,
                                      curator, bot, publisher, token
                                      refresher

  EventBridge                         S3 ObjectCreated → indexer

  EventBridge Scheduler               Daily at 09:00 local; one-time
                                      scheduled posts

  Lambda Function URL                 Telegram webhook

  Amazon Location Service             Place lookup / reverse geocoding

  SSM Parameter Store (SecureString)  Telegram bot token, webhook secret,
                                      owner chat ID, Instagram token,
                                      user ID, home coordinates

  SQS DLQ + CloudWatch alarms         S3/Lambda failures, token refresh
                                      failures, DLQ depth; alert via
                                      Telegram

  IAM user                            PhotoSync upload-only credentials

  SNS topic (us-east-1)               `photo-sync-cost-alerts`, created
                                      outside CDK; import it. Receives
                                      Cost Anomaly and Budgets alerts
  -----------------------------------------------------------------------

Cost controls (created outside CDK): AWS Budget `photo-sync-monthly`
($10/month; alerts at 50% actual, 100% actual, 100% forecasted, emailed
to both owner addresses) and Cost Anomaly Detection (Default-Services-
Monitor, $5 impact, immediate via the SNS topic above).

Estimated AWS cost: less than \$1/month (most usage within free tier).
All-in with Claude: approximately \$2--5/month, plus the PhotoSync
Premium license.

Infrastructure as code: AWS CDK (Python).

## 9. Security and privacy

-   Personal AWS account; bucket fully private. Instagram fetches
    renders only through short-lived presigned URLs.
-   Renders carry no EXIF/GPS; captions never include street addresses.
-   Least privilege per Lambda; the phone's credentials can only upload.
-   Telegram webhook authenticated by secret header and chat ID
    allowlist.
-   Secrets only in SSM; nothing secret in code or environment
    variables.

## 10. Repository layout

``` text
photo_sync/
├── infrastructure/          # CDK app (stacks: storage, pipeline, bot)
├── photo_sync/
│   ├── indexer.py
│   ├── evaluator.py          # collections + readiness (pure functions, unit-tested)
│   ├── curator.py
│   ├── renderer.py
│   ├── publisher.py
│   └── common/               # config, DynamoDB, S3, Claude, Telegram, Instagram clients
├── tests/
├── Dockerfile                # shared Lambda image
└── pyproject.toml
```

## 11. Configuration defaults

| Key | Value |
|-----|-------|
| `INGEST_START_DATE` | PhotoSync install date (to be set) |
| `TIMEZONE` | America/New_York |
| `HOME_ZONES` | JSON list of `{lat, lon, radius_km}` in `.env` (private; two zones configured) |
| `MIN_PHOTOS_MONTHLY_DUMP` | 10 |
| `MIN_PHOTOS_TRIP` | 3 |
| `MIN_PHOTOS_EVENT` | 3 |
| `EVENT_GAP_HOURS` | 3 |
| `EVENT_RADIUS_KM` | 5 |
| `EVENT_CLOSE_HOURS` | 24 |
| `TRIP_CLOSE_HOURS` | 24 |
| `GPS_INFER_WINDOW_HOURS` | 12 |
| `CAROUSEL_RATIO` | 4:5 |
| `CAPTION_LANGUAGE` / `HASHTAG_COUNT` | English / 3 |
| `CAPTIONS_PER_ROUND` | 3 |
| `CAPTION_EXAMPLES_LIKED` / `CAPTION_EXAMPLES_DISLIKED` | 10 / 10 |
| `TAGGING_MODEL` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| `CURATOR_MODEL` | `us.anthropic.claude-opus-4-6-v1` |
| `CAPTION_MODEL` | same as `CURATOR_MODEL` (configurable) |
| `PROPOSAL_EXPIRY_DAYS` | 7 |
| `RENDER_RETENTION_DAYS` | 30 |
| `MONTHLY_BUDGET_USD` | 10 |

## 12. Delivery plan

| Phase | Work | Done when |
|-------|------|-----------|
| 0 --- Setup | AWS account, bucket, upload IAM user, PhotoSync; Bedrock access to Opus and Haiku in the chosen region; create the Instagram account, convert to Creator, set up Meta developer account, app, and tester role; SHA-256 de-duplication | A test original matches the S3 object; both Claude models answer in the Bedrock playground; a hand-made JPEG publishes to the new account via a presigned URL (also tests location tags) |
| 1 --- Monthly dump and ingest | Phone upload, S3 event, indexer, metadata and derivatives | A monthly dump arrives and indexes |
| 2 --- Events and trips | Event and trip detection, GPS inference, daily evaluator, collection membership and readiness | A real event/trip is grouped correctly |
| 3 --- Curation and approval | Claude selection, ordering, crop and caption; Telegram preview and feedback; cost-alert forwarding from SNS | Approving a proposal produces the intended post |
| 4 --- Publishing | Instagram container creation, polling, publishing, location tags, token refresh and scheduled posts | An approved post publishes successfully |

## 13. Decisions and open questions

### Decided

-   Minimums: monthly dump 10, trip 3, event 3. No priority between
    types; every ready collection is sent. No cooldown.
-   A trip is any photo outside the home zones by GPS. Photos without GPS take the location of photos within 12 hours.
    Timezone: Eastern.
-   PhotoSync uploads from the install date onward only.
-   Telegram preview quality is acceptable as long as the approved
    render is what is posted; carousels over 10 images use several
    messages.
-   Home-area GPS is never in captions. Posts include location tags.
    Stories are a later feature. Still images only until Phase 3.
-   The Instagram account is new: no history import. Captions are
    English with 3 hashtags.
-   Captions: 3 options per round, no round limit. Learning uses only
    the last 10 liked and last 10 disliked captions in the prompt; no
    voice guide. Updates to the pools need your approval, shown per post
    type (§5.9).
-   Region: us-east-1. Home zones are circles (1 km and 2 km) around two
    anchors geocoded once. Coordinates, the bucket name, and credentials
    live only in `.env` (gitignored; `.env.example` lists the keys). CDK
    reads `HOME_ZONES` and `BUCKET` from it and stores the zones in SSM.

### Open

1.  Bedrock access to newer models (Haiku 5.5, Opus 4.7 and later) is
    blocked on the account; request access via AWS Support. Haiku 4.5
    and Opus 4.6 already work.
2.  Optional: 3--5 sample captions to seed the liked pool.
3.  Whether location tags work through the Instagram Login API
    (Phase 0 test).
4.  iCloud settings: Optimize iPhone Storage; Advanced Data Protection?
    PhotoSync downloads originals when needed.

### Proposed defaults, not yet confirmed

-   "Disliked" means a candidate you criticized or replaced, or any
    candidate of a rejected proposal; options unpicked in the approved
    round are neutral.
-   Pending learning summaries wait for your reply and never expire.
-   Events are clusters inside the home zone; trips are runs outside it.
-   Trip and event close after 24 hours without photos (a trip also
    closes on return to the home zone).
-   Monthly dump fires after the calendar month ends.
-   Carousel ratio 4:5 for every slide.
-   Scheduled posts at most 14 days out.
-   A "never use this photo" Telegram button; no face recognition.
-   Alt text generated; user tags out of scope.
-   Bursts and RAW ignored.
