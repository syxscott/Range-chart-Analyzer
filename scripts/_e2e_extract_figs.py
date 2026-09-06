# Extract geological information figures from radiolarian papers.
# Picks 40 papers, locates chart-like figure captions, saves the largest
# embedded image on those pages, and writes a manifest for evaluation.
import fitz, os, re, json, hashlib, sys

sys.stdout.reconfigure(encoding='utf-8')

BASE = r"D:\文档\编程项目\SKills寻找创新点\2021新放射虫文献"
OUT = os.path.join("outputs", "e2e_radiolaria")
os.makedirs(OUT, exist_ok=True)

# Caption keywords that indicate a geological information figure.
CAP_EN = re.compile(
    r"(range\s*chart|distribution|abundance|zonation|correlation|columnar|"
    r"litholog|stratigraphic\s*column|biostratigraph|frequency|percentage\s*diagram)", re.I)
CAP_ZH = re.compile(r"(延限|分布|丰度|对比|柱状|地层柱|图式|组合带|延限图)")
CAP_FIG = re.compile(r"^(Fig\.?\s*\d+|Figure\s*\d+|图\s*[0-9一二三四五六七八九十]+|表\s*[0-9一二三四五六七八九十]+|Pl\.\s*[IVX\d]+|Plate)", re.I)

def iter_pdfs():
    for root, _dirs, files in os.walk(BASE):
        for f in files:
            if f.lower().endswith(".pdf"):
                yield os.path.join(root, f)

manifest = []
papers_used = set()
seen_hashes = set()
scanned = 0

for path in iter_pdfs():
    if len(manifest) >= 60:  # safety cap while collecting
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
    # find pages with chart-like captions
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
        if taken >= 2 or len(manifest) >= 60:
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
        # normalize to RGB (some CMYK / indexed / alpha pixmaps fail as PNG)
        try:
            if pix.n != 3 or (pix.colorspace and pix.colorspace.n not in (1, 3)):
                pix = fitz.Pixmap(fitz.csRGB, pix)
            h = hashlib.md5(pix.tobytes("png")).hexdigest()[:10]
            if h in seen_hashes:
                pix = None
                continue
            seen_hashes.add(h)
            name = f"fig_{len(manifest):02d}_{h}.png"
            out_path = os.path.join(OUT, name)
            pix.save(out_path)
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

# keep exactly 40
manifest = manifest[:40]
with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=1)

print(f"papers scanned: {scanned}, figures kept: {len(manifest)}, distinct papers: {len(papers_used)}")
for m in manifest[:40]:
    print(f"  {m['image']}  p{m['page']:>3}  {m['caption'][:80]}")
