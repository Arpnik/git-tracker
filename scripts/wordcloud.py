#!/usr/bin/env python3
"""
Language word-cloud renderer.

Builds a self-contained D3 (+ d3-cloud) HTML page where each word is one of the
languages you've worked in, sized proportionally to the lines of code changed in
it. The words are masked into the shape of an image pulled from your `my_face`
repo, so the cloud reads as "a picture of you, made of your languages".

The face image is downloaded and embedded as a base64 data URI, so the output
file is fully self-contained (works offline / on GitHub Pages, even for a
private my_face repo).
"""
import json
import base64

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
}


def _fetch_face_data_uri(session, api, owner, repo="my_face"):
    """Find the first image in `owner/my_face` and return it as a data URI."""
    r = session.get(f"{api}/repos/{owner}/{repo}/contents")
    if r.status_code != 200:
        print(f"  ! word cloud: could not read {owner}/{repo} contents "
              f"(HTTP {r.status_code}); rendering without a face mask.")
        return None
    for item in sorted(r.json(), key=lambda i: i.get("name", "")):
        name = item.get("name", "").lower()
        if item.get("type") == "file" and name.endswith(IMAGE_EXTS):
            dl = item.get("download_url")
            if not dl:
                continue
            ir = session.get(dl)
            if ir.status_code != 200:
                continue
            ext = next((e for e in IMAGE_EXTS if name.endswith(e)), ".png")
            mime = _MIME.get(ext, "image/png")
            b64 = base64.b64encode(ir.content).decode()
            print(f"  + word cloud: using face image '{item['name']}' from {owner}/{repo}.")
            return f"data:{mime};base64,{b64}"
    print(f"  ! word cloud: no image found in {owner}/{repo}; rendering without a face mask.")
    return None


def render_language_wordcloud(stats, session, api, owner, out_path, repo="my_face"):
    """Write the word-cloud HTML to out_path. Returns True if a face mask was used."""
    image_uri = _fetch_face_data_uri(session, api, owner, repo)
    words = [
        {"text": lang, "size": s["additions"] + s["deletions"]}
        for lang, s in stats.get("by_language", {}).items()
        if (s["additions"] + s["deletions"]) > 0
    ]
    html = (_TEMPLATE
            .replace("__WORDS__", json.dumps(words))
            .replace("__IMAGE__", image_uri or ""))
    out_path.write_text(html)
    return image_uri is not None


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Language word cloud</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body{margin:0;background:#0d1117;color:#c9d1d9;font-family:system-ui,-apple-system,sans-serif;text-align:center}
  h1{font-size:15px;font-weight:600;padding:18px 16px 4px;letter-spacing:.2px}
  .sub{color:#6e7681;font-size:12px;padding-bottom:12px}
  #cloud{max-width:840px;margin:0 auto}
  svg{width:100%;height:auto}
</style>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3-cloud/1.2.7/d3.layout.cloud.min.js"></script>
</head>
<body>
<h1>languages i've been working on — a picture made of words</h1>
<div class="sub">word size ∝ lines of code changed in that language</div>
<div id="cloud"></div>
<script>
const WORDS = __WORDS__;
const IMAGE = "__IMAGE__";
const W = 840, H = 840;
const total = d3.sum(WORDS, d => d.size) || 1;
const maxSize = d3.max(WORDS, d => d.size) || 1;
const fontScale = d3.scaleSqrt().domain([0, maxSize]).range([12, 120]);
const color = d3.scaleOrdinal(d3.schemeTableau10);

d3.layout.cloud()
  .size([W, H])
  .words(WORDS.map(d => ({
    text: d.text, size: fontScale(d.size), loc: d.size, pct: d.size / total * 100
  })))
  .padding(2)
  .rotate(() => (Math.random() < 0.5 ? 0 : 90))
  .font("Impact")
  .fontSize(d => d.size)
  .on("end", draw)
  .start();

function draw(words) {
  const svg = d3.select("#cloud").append("svg").attr("viewBox", `0 0 ${W} ${H}`);
  const defs = svg.append("defs");

  if (IMAGE) {
    // Luminance mask: words only show where the face image is bright.
    const mask = defs.append("mask").attr("id", "face")
      .attr("maskUnits", "userSpaceOnUse")
      .attr("x", 0).attr("y", 0).attr("width", W).attr("height", H);
    mask.append("rect").attr("width", W).attr("height", H).attr("fill", "black");
    mask.append("image").attr("href", IMAGE).attr("x", 0).attr("y", 0)
      .attr("width", W).attr("height", H)
      .attr("preserveAspectRatio", "xMidYMid slice");
    // Faint ghost of the image so the shape is readable behind the words.
    svg.append("image").attr("href", IMAGE)
      .attr("width", W).attr("height", H)
      .attr("preserveAspectRatio", "xMidYMid slice").attr("opacity", 0.06);
  }

  const g = svg.append("g");
  if (IMAGE) g.attr("mask", "url(#face)");

  const text = g.selectAll("text").data(words).enter().append("text")
    .style("font-size", d => d.size + "px")
    .style("font-family", "Impact")
    .attr("fill", d => color(d.text))
    .attr("text-anchor", "middle")
    .attr("transform", d => `translate(${d.x + W / 2},${d.y + H / 2}) rotate(${d.rotate})`)
    .text(d => d.text);
  text.append("title").text(d => `${d.text}: ${d.loc.toLocaleString()} lines (${d.pct.toFixed(1)}%)`);
}
</script>
</body>
</html>"""

