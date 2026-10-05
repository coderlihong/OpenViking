# Office conversion worker

The image bundles the product `OfficeConverter` and its used-sheet policy. It is
a bounded command-line worker. The OpenViking HTTP/SDK ingestion route remains
the public entry point. The explicit SSH transport below connects the frozen remote worker.

The tested deployment lives at `/home/t-wanghai/models/openviking-office` on
`10.147.254.3`. Six synthetic formats passed there on 2026-10-05 with
`LibreOffice 7.4.7.2 40(Build:2)`. The immutable local image ID and font inventory
are in `lock.json`; Debian package versions are recorded in
`docs/spec/evidence/office-container-packages-2026-10-05.txt`. The Dockerfile
pins the base digest and Python package versions. A rebuild must create a new
lock and rerun the probes because distribution packages may change.

Run from a host with that frozen image present:

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --memory 4g --cpus 2 --pids-limit 128 \
  --tmpfs /tmp:rw,nosuid,nodev,size=1g \
  -v /absolute/office-input:/input:ro \
  -v /absolute/converter-output:/output:rw \
  sha256:6a2cfcf932ad67e7f0445d50b87e446e0b5c9710053eee27324d54d06201cc70 \
  /input/document.docx /output/document.pdf \
  --expected-version 'LibreOffice 7.4.7.2 40(Build:2)'
```

The output directory must be writable by UID/GID 10001. The caller owns the
copied PDF's retention policy; the converter removes its private profile and
intermediate files, while retaining the read-only original.

Each job has a fresh profile with macro execution and active content disabled.
The subprocess uses CPU and file-size limits, Linux address-space limits, a
wall-clock timeout and process-group cancellation. LibreOffice's documented
`MAX_CONCURRENCY` is fixed at 2 so a high-core server does not allocate hundreds
of conversion threads. This setting is documented in the
[official Calc guide](https://documentation.libreoffice.org/assets/Uploads/Documentation/en/CG7.2/CG72-CalcGuide.pdf).
macOS supports the CPU/file checks used in development, but Linux containers
provide the tested memory boundary.

For Excel, source print areas are replaced with each used sheet's rendered
range. Non-empty hidden sheets are included, empty sheets are skipped, narrow
columns gain readable widths and wrapping, and sheet names appear in headers.
Legacy XLS first converts to XLSX before this policy is applied. Formulas are
rendered by LibreOffice; spreadsheet execution semantics are not guaranteed.

Run the synthetic contract probe with the same containment flags, overriding
the entry point to `python3` and mounting `probe.py` as `/probe.py`:

```sh
# Append to the containment/mount options above:
--entrypoint python3 IMAGE_ID /probe.py /input /output
```

The six fixtures originate in `tests/parse/test_office_used_sheets.py`.
`probe.py` checks text, table/unit tokens, images, Excel tails/hidden sheets,
slide order, source hashes, real LibreOffice timeout and cancellation cleanup.
These fixtures are synthetic contract probes. They do not satisfy independent
Office quality gold or complete ingestion/retrieval FLOW acceptance.


### SSH transport for an OpenViking host without LibreOffice

`ssh-soffice` implements the converter's command protocol using authenticated
SSH key access. It transfers each inspected input to a disposable Docker
container on the tested server; only the PDF (or the intermediate XLSX for
legacy Excel) comes back. The same product converter still performs source
inspection, Excel used-sheet normalization, output validation, metadata, and
local cleanup. No model or ingestion API is added.

Install `transport_worker.py` at
`/home/t-wanghai/models/openviking-office/transport_worker.py` on the tested
server. Keep the image in `lock.json` present. The service's account must have
working SSH key access with host verification configured before startup.
Make the local `ssh-soffice` file executable, then start OpenViking with:

```sh
export OPENVIKING_OFFICE_BINARY=/Users/wh/code/OpenViking/deploy/office-converter/ssh-soffice
export OPENVIKING_OFFICE_EXPECTED_VERSION='LibreOffice 7.4.7.2 40(Build:2)'
export OPENVIKING_OFFICE_FONT_INVENTORY_FROM_BINARY=true
```

Both the parser router and durable job preflight construct the converter with
these explicit environment overrides. Invalid/nonexecutable paths are rejected.
`OPENVIKING_OFFICE_SSH_HOST` and `OPENVIKING_OFFICE_SSH_WORKER` optionally change
the SSH destination and absolute remote worker path. Credentials do not belong
in these variables. Restart the service to apply a changed deployment.

Font checks remain enabled and use the actual remote container inventory,
including Noto CJK. Calibri, Arial, and other unavailable declared fonts cause
`missing_fonts`. For synthetic contract probes only, the explicit override
`OPENVIKING_OFFICE_CHECK_FONTS=false` allows substitution; it cannot establish
quality acceptance. Empty or failed remote font inventory fails closed.

The remote container runs as the SSH account's UID/GID, with network disabled,
read-only root, dropped capabilities, CPU/memory/file/process limits and a
fresh macro-disabled profile. Inputs/profile/output are inside a private 0700
request directory. The 180-second remote deadline remains active if the local
caller is cancelled or disconnects, and the worker finally removes its
container and request directory. The host conversion timeout can expire before
remote cleanup finishes; callers must not interpret local cancellation as an
instantaneous remote cancellation acknowledgement.

Six synthetic sources passed this SSH transport with the actual frozen image;
see `transport-probe-2026-10-05.json`. This proves conversion contracts, not
independent gold, Office ingestion/retrieval FLOW, or complete KB-012.

Font detection reads namespace-aware OOXML attributes, SpreadsheetML font name
children, and DrawingML major/minor/theme script declarations. Theme aliases
such as `+mn-lt` are not treated as literal font families. The inventory gate
is conservative over declarations; it does not yet prove which declared
styles/scripts supply the actual used glyphs.
