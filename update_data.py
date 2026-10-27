#!/usr/bin/env python3
"""Robot de mise à jour : récupère des données publiques gratuites et écrit data/macro.json.
Chaque source est isolée : si l'une échoue, les autres continuent et l'échec est noté dans le fichier.
Aucune clé requise. Bibliothèque standard uniquement.
Test hors ligne : MF_MOCK_DIR=<dossier> lit des fichiers au lieu d'Internet."""
import csv, io, json, os, re, sys, time, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

OUT = os.environ.get("MF_OUT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "macro.json"))
MOCK = os.environ.get("MF_MOCK_DIR")
UA = "Mozilla/5.0 (compatible; macro-forex-robot; +https://github.com/philippembulu1-dot/macro-forex)"
NOW = datetime.now(timezone.utc)
SOURCES = []


def http(key, url, tries=3):
    if MOCK:
        p = os.path.join(MOCK, key)
        if not os.path.exists(p):
            raise IOError("mock absent: " + key)
        with open(p, encoding="utf-8") as f:
            return f.read()
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa
            last = e
            time.sleep(2 + 3 * i)
    raise last


def note(sid, label, kind, ok, asof=None, err=None):
    SOURCES.append({"id": sid, "label": label, "kind": kind, "ok": bool(ok), "asof": asof, "err": (str(err)[:160] if err else None)})


# ---------------- FRED (CSV public, sans clé) ----------------
FRED_DAILY = [
    ("DGS10", "Rendement US 10 ans"), ("DGS2", "Rendement US 2 ans"), ("T10Y2Y", "Écart 10 ans − 2 ans"),
    ("DFII10", "Rendement réel 10 ans (TIPS)"), ("VIXCLS", "VIX"), ("DFEDTARU", "Fed : borne haute"),
    ("DFEDTARL", "Fed : borne basse"), ("ECBDFR", "BCE : taux de dépôt"), ("DTWEXBGS", "Dollar large (indice Fed)"),
]
FRED_MONTHLY = [("CPIAUCSL", "IPC US"), ("CPILFESL", "IPC US sous-jacent"), ("UNRATE", "Chômage US"), ("PAYEMS", "Emplois US")]
# Séries OECD relayées par FRED : retard de plusieurs semaines, donc indicatives seulement.
OECD_RATES = [("GB", "BoE"), ("JP", "BoJ"), ("CH", "BNS"), ("AU", "RBA"), ("CA", "BoC"), ("NZ", "RBNZ")]
OECD_CPI = [("EZ", "zone euro"), ("GB", "Royaume-Uni"), ("JP", "Japon"), ("CH", "Suisse"), ("CA", "Canada"), ("AU", "Australie"), ("NZ", "Nouvelle-Zélande")]


def fred(sid, start):
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s" % (sid, start)
    txt = http("fred_" + sid + ".csv", url)
    rows = list(csv.reader(io.StringIO(txt)))
    out = []
    for r in rows[1:]:
        if len(r) < 2:
            continue
        try:
            out.append((r[0].strip(), float(r[1])))
        except ValueError:
            pass
    if not out:
        raise ValueError("série vide")
    return out


def year_ago(obs, mode="m"):
    last = obs[-1]
    y, m = last[0][:4], last[0][5:7]
    tgt = "%d-%s" % (int(y) - 1, m)
    for d, v in obs:
        if d[:7] == tgt:
            return v
    return None


def main():
    data = {"fred": {}, "derived": {}, "oecd": {}, "cot": {}, "news": []}
    start_d = (NOW - timedelta(days=200)).strftime("%Y-%m-%d")
    start_m = (NOW - timedelta(days=900)).strftime("%Y-%m-%d")

    for sid, label in FRED_DAILY:
        try:
            o = fred(sid, start_d)
            data["fred"][sid] = {"v": o[-1][1], "prev": o[-2][1] if len(o) > 1 else None, "asof": o[-1][0], "hist": [[d, v] for d, v in o[-90:]]}
            note("fred." + sid, label, "daily", True, o[-1][0])
        except Exception as e:
            note("fred." + sid, label, "daily", False, err=e)

    raw = {}
    for sid, label in FRED_MONTHLY:
        try:
            raw[sid] = fred(sid, start_m)
            note("fred." + sid, label, "monthly", True, raw[sid][-1][0])
        except Exception as e:
            note("fred." + sid, label, "monthly", False, err=e)
    d = data["derived"]
    for sid, key in (("CPIAUCSL", "us_cpi"), ("CPILFESL", "us_core")):
        o = raw.get(sid)
        if o and len(o) > 13:
            ya = year_ago(o)
            prev_o = o[:-1]
            ya2 = year_ago(prev_o) if prev_o else None
            if ya:
                d[key] = {"v": round((o[-1][1] / ya - 1) * 100, 2), "prev": round((o[-2][1] / ya2 - 1) * 100, 2) if ya2 else None, "asof": o[-1][0]}
    if raw.get("UNRATE"):
        o = raw["UNRATE"]
        d["us_unemp"] = {"v": o[-1][1], "prev": o[-2][1], "asof": o[-1][0]}
    if raw.get("PAYEMS") and len(raw["PAYEMS"]) > 2:
        o = raw["PAYEMS"]
        d["us_nfp"] = {"v": round(o[-1][1] - o[-2][1]), "prev": round(o[-2][1] - o[-3][1]), "asof": o[-1][0]}

    for cc, name in OECD_RATES:
        sid = "IRSTCB01%sM156N" % cc
        try:
            o = fred(sid, start_m)
            data["oecd"]["rate_" + cc] = {"v": o[-1][1], "prev": o[-2][1] if len(o) > 1 else None, "asof": o[-1][0]}
            note("oecd.rate." + cc, "Taux directeur " + name + " (OCDE, retard)", "monthly", True, o[-1][0])
        except Exception as e:
            note("oecd.rate." + cc, "Taux directeur " + name + " (OCDE, retard)", "monthly", False, err=e)
    for cc, name in OECD_CPI:
        got = False
        for per in ("M", "Q"):
            sid = "CPALTT01%s%s659N" % (cc, per)
            try:
                o = fred(sid, start_m)
                data["oecd"]["cpi_" + cc] = {"v": o[-1][1], "prev": o[-2][1] if len(o) > 1 else None, "asof": o[-1][0]}
                note("oecd.cpi." + cc, "Inflation " + name + " (OCDE, retard)", "monthly" if per == "M" else "quarterly", True, o[-1][0])
                got = True
                break
            except Exception as e:
                err = e
        if not got:
            note("oecd.cpi." + cc, "Inflation " + name + " (OCDE, retard)", "monthly", False, err=err)

    # ---------------- COT (CFTC, rapport « Legacy », futures seuls) ----------------
    CODES = {"088691": "XAU", "099741": "EUR", "096742": "GBP", "097741": "JPY", "092741": "CHF", "232741": "AUD", "090741": "CAD", "112741": "NZD"}
    try:
        where = "cftc_contract_market_code in(%s)" % ",".join("'%s'" % c for c in CODES)
        q = urllib.parse.urlencode({"$where": where, "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": "80"})
        txt = http("cot.json", "https://publicreporting.cftc.gov/resource/6dca-aqww.json?" + q)
        rows = json.loads(txt)
        by = {}
        for r in rows:
            code = r.get("cftc_contract_market_code")
            if code in CODES:
                by.setdefault(CODES[code], []).append(r)
        newest = None
        for k, lst in by.items():
            lst.sort(key=lambda r: r.get("report_date_as_yyyy_mm_dd", ""), reverse=True)
            def net(r):
                return int(float(r["noncomm_positions_long_all"])) - int(float(r["noncomm_positions_short_all"]))
            cur = lst[0]
            n0 = net(cur)
            n1 = net(lst[1]) if len(lst) > 1 else None
            data["cot"][k] = {"date": cur["report_date_as_yyyy_mm_dd"][:10], "long": int(float(cur["noncomm_positions_long_all"])),
                              "short": int(float(cur["noncomm_positions_short_all"])), "net": n0, "prev_net": n1,
                              "oi": int(float(cur.get("open_interest_all", 0) or 0)), "hist": [net(r) for r in lst[:12]][::-1]}
            if not newest or data["cot"][k]["date"] > newest:
                newest = data["cot"][k]["date"]
        if not data["cot"]:
            raise ValueError("aucune ligne COT")
        note("cftc.cot", "COT CFTC (positions des fonds)", "weekly", True, newest)
    except Exception as e:
        note("cftc.cot", "COT CFTC (positions des fonds)", "weekly", False, err=e)

    # ---------------- Actualités officielles des banques centrales (RSS) ----------------
    FEEDS = [
        ("Fed", "USD", "https://www.federalreserve.gov/feeds/press_all.xml"),
        ("BCE", "EUR", "https://www.ecb.europa.eu/rss/press.html"),
        ("BoE", "GBP", "https://www.bankofengland.co.uk/rss/news"),
        ("BoJ", "JPY", "https://www.boj.or.jp/en/rss/whatsnew.xml"),
        ("BNS", "CHF", "https://www.snb.ch/public/en/rss/pressrel"),
        ("RBA", "AUD", "https://www.rba.gov.au/rss/rss-cb-media-releases.xml"),
        ("BoC", "CAD", "https://www.bankofcanada.ca/content_type/press-releases/feed/"),
    ]
    items = []
    for name, cur_, url in FEEDS:
        try:
            txt = http("rss_" + name + ".xml", url, tries=2)
            root = ET.fromstring(txt.encode("utf-8"))
            n = 0
            for it in list(root.iter("item")) + list(root.iter("{http://www.w3.org/2005/Atom}entry")):
                def g(*tags):
                    for tag in tags:
                        for full in (tag, "{http://www.w3.org/2005/Atom}" + tag, "{http://purl.org/dc/elements/1.1/}" + tag):
                            e = it.find(full)
                            if e is not None:
                                return e
                    return None
                t = g("title"); l = g("link"); dt = g("pubDate", "updated", "published", "date")
                if t is None or not (t.text or "").strip():
                    continue
                link = (l.text or "").strip() if l is not None and l.text else (l.get("href") if l is not None else "")
                items.append({"src": name, "c": cur_, "t": re.sub(r"\s+", " ", t.text).strip()[:200], "l": link, "d": parse_date((dt.text or "").strip() if dt is not None else "")})
                n += 1
                if n >= 6:
                    break
            if n == 0:
                raise ValueError("flux vide")
            note("rss." + name, "Communiqués " + name, "news", True, max((i["d"] for i in items if i["src"] == name and i["d"]), default=None))
        except Exception as e:
            note("rss." + name, "Communiqués " + name, "news", False, err=e)
    items.sort(key=lambda i: i["d"] or "", reverse=True)
    data["news"] = items[:40]

    out = {"generated": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "sources": SOURCES}
    out.update(data)
    os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    ok = sum(1 for s in SOURCES if s["ok"])
    print("OK %d/%d sources" % (ok, len(SOURCES)))
    for s in SOURCES:
        if not s["ok"]:
            print("  échec", s["id"], s["err"])
    if ok == 0:
        sys.exit(1)  # rien récupéré : on ne remplace pas l'ancien fichier


def parse_date(s):
    if not s:
        return None
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
        return m.group(1) if m else None


if __name__ == "__main__":
    main()
