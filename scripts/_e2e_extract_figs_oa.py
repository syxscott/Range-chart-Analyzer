# Extract geological information figures from a RANDOM sample of papers.
# Usage:
#   python scripts/_e2e_extract_figs_oa.py <base_dir> <out_dir> [count] [per_paper] [seed]
# Random sample is seeded so the selection is reproducible.
import fitz, os, re, json, hashlib, random, sys

sys.stdout.reconfigure(encoding='utf-8')

BASE = sys.argv[1]
OUT = sys.argv[2]
COUNT = int(sys.argv[3]) if len(sys.argv) > 3 else 40
PER_PAPER = int(sys.argv[4]) if len(sys.argv) > 4 else 3
SEED = int(sys.argv[5]) if len(sys.argv) > 5 else 20260907
os.makedirs(OUT, exist_ok=True)

CAP_EN = re.compile(
    r"(range\s*chart|distribution|abundance|zonation|correlation|columnar|"
    r"litholog|stratigraphic\s*column|biostratigraph|frequency|percentage\s*diagram)", re.I)
CAP_ZH = re.compile(r"(延限|分布|丰度|对比|柱状|地层柱|图式|组合带|延限图)")
CAP_FIG = re.compile(r"^(Fig\.?\s*\d+|Figure\s*\d+|Plate\s+[IVXLC\d]+|图\s*[0-9一二三四五六七八九十]+|表\s*[0-9一二三四五六七八九十]+|Pl\.\s*[IVX\d]+|Plate)", re.I)

pdfs = []
for root, _dirs, files in os.walk(BASE):
    for f in files:
        if f.lower().endswith(".pdf"):
            pdfs.append(os.path.join(root, f))

rng = random.Random(SEED)
rng.shuffle(pdfs)
print(f"total PDFs: {len(pdfs)}; random sample target: {COUNT} papers "
      f"(seed={SEED}, per-paper cap={PER_PAPER})", flush=True)

manifest = []
papers_used = set()
seen_hashes = set()
scanned = 0
done = False

for path in pdfs:
    if done or len(papers_used) >= COUNT:
        break
    rel = os.path.relpath(path, BASE)
    paper = os.path.splitext(os.path.basename(path))[0][:70]
    if paper in papers_used:
        continue
    try:
        doc = fitz.open(path)
    except Exception:
        continue
    if len(doc) < 3:
        doc.close(); continue
    scanned += 1
    hits = []
    for pno in range(len(doc)):
        try:
            text = doc[pno].get_text()
        except Exception:
            continue
        if not text or len(text) < 50:
            continue
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            if CAP_FIG.match(s) and (CAP_EN.search(s) or CAP_ZH.search(s)):
                hits.append((pno, s[:120]))
                break
    if not hits:
        doc.close(); continue

    taken = 0
    for pno, cap in hits:
        if taken >= PER_PAPER or len(papers_used) >= COUNT:
            break
        try:
            imgs = doc[pno].get_images(full=True)
        except Exception:
            continue
        best = None
        for im in imgs:
            xref = im[0]
            try:
                pix = fitz.Pixmap(doc, xref)
            except Exception:
                continue
            if pix.width < 300 or pix.height < 300:
                pix = None; continue
            area = pix.width * pix.height
            if best is None or area > best[1]:
                best = (pix, area)
        if best is None:
            continue
        pix = best[0]
        try:
            if pix.n != 3 or (pix.colorspace and pix.colorspace.n not in (1, 3)):
                pix = fitz.Pixmap(fitz.csRGB, pix)
            # UI-REVIEW-2026-09-08 (E2E oa_029): some CMYK/JPX embedded
            # images convert to an all-black pixmap. Detect the degenerate
            # conversion and fall back to rendering the WHOLE PAGE instead
            # (page rendering is always correct and keeps the caption in
            # frame, which helps the model).
            samples = pix.samples
            if samples and samples.count(samples[0]) == len(samples):
                pix = doc[pno].get_pixmap(dpi=150)
            h = hashlib.md5(pix.tobytes("png")).hexdigest()[:10]
            if h in seen_hashes:
                pix = None
                continue
            seen_hashes.add(h)
            name = f"oa_{len(manifest):03d}_{h}.png"
            pix.save(os.path.join(OUT, name))
        except Exception:
            pix = None
            continue
        pix = None
        manifest.append({
            "image": name,
            "paper": paper,
            "pdf_rel": rel,
            "page": pno + 1,
            "caption": cap,
        })
        papers_used.add(paper)
        taken += 1
    doc.close()
    if len(manifest) % 10 == 0:
        print(f"  progress: {len(manifest)} figures from {len(papers_used)} papers", flush=True)

with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=1)

print(f"DONE papers scanned: {scanned}, figures kept: {len(manifest)}, "
      f"distinct papers: {len(papers_used)}", flush=True)
