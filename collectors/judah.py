from __future__ import annotations

import io, json, re, time
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from pypdf import PdfReader

SCHOOL_ID = "judah"
CALENDAR_PAGE = "https://www.judah.org/calendar"
ATHLETICS_PAGE = "https://arbiterlive.com/School/Calendar/11489"
CALENDAR_SOURCE_NAME = "Judah Christian School Calendar"
ATHLETICS_SOURCE_NAME = "Judah Christian Athletics"
CURRENT_PDF_FALLBACK = "https://www.judah.org/_files/ugd/5c231c_aa1c4cab08b741c2a37fbb94cbecfdad.pdf"
CHICAGO = ZoneInfo("America/Chicago")

MONTHS = {name.lower(): i for i,name in enumerate(
    ["January","February","March","April","May","June","July","August","September","October","November","December"],1)}
MONTH_RE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\b",re.I)
EVENT_START_RE = re.compile(r"^\s*(?P<d1>3[01]|[12]\d|0?[1-9])(?:\s*-\s*(?P<d2>3[01]|[12]\d|0?[1-9]))?\s+(?P<title>.*\S)\s*$")
INLINE_RANGE_RE = re.compile(r"\b(?P<m1>1[0-2]|0?[1-9])/(?P<d1>3[01]|[12]\d|0?[1-9])\s*-\s*(?:(?P<m2>1[0-2]|0?[1-9])/)?(?P<d2>3[01]|[12]\d|0?[1-9])\b")

@dataclass(frozen=True)
class PdfCandidate:
    url:str
    label:str=""

class _CalendarLinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.links=[]; self.href=None; self.parts=[]
    def handle_starttag(self,tag,attrs):
        a={str(k).casefold():str(v) for k,v in attrs if k and v is not None}
        if tag.casefold()=="a": self.href=a.get("href"); self.parts=[]
        for k in ("href","src","data-src"):
            v=a.get(k)
            if v and ".pdf" in v.casefold(): self.links.append(PdfCandidate(v,""))
    def handle_data(self,data):
        if self.href is not None:
            t=re.sub(r"\s+"," ",data).strip()
            if t:self.parts.append(t)
    def handle_endtag(self,tag):
        if tag.casefold()=="a" and self.href is not None:
            if ".pdf" in self.href.casefold(): self.links.append(PdfCandidate(self.href," ".join(self.parts)))
            self.href=None; self.parts=[]

def _request_bytes(url,*,timeout=25,accept="*/*",opener=urlopen):
    req=Request(url,headers={"User-Agent":"ChambanaSchoolboard/1.0 (+public school calendar aggregator)","Accept":accept})
    with opener(req,timeout=timeout) as r:return r.read()

def _academic_year(reference:date,hint=""):
    m=re.search(r"\b(20\d{2})\s*[-–—/]\s*(20\d{2}|\d{2})\b",hint)
    if m:
        a=int(m.group(1)); b=int(m.group(2)); b=(a//100)*100+b if len(m.group(2))==2 else b
        if b==a+1:return a,b
    return (reference.year,reference.year+1) if reference.month>=7 else (reference.year-1,reference.year)

def _discover_pdf_from_html(html,*,reference):
    decoded=unescape(html).replace(r"\\/","/")
    p=_CalendarLinkParser(); p.feed(decoded)

    for u in re.findall(r'https?://[^"\'<>\\\s]+?\.pdf(?:\?[^"\'<>\\\s]*)?',decoded,re.I):
        p.links.append(PdfCandidate(u,""))
    for u in re.findall(r'["\']([^"\']*?_files/ugd/[^"\']+?\.pdf(?:\?[^"\']*)?)["\']',decoded,re.I):
        p.links.append(PdfCandidate(u,""))

    by={}
    for item in p.links:
        full=urljoin(CALENDAR_PAGE,item.url)
        existing=by.get(full)
        if existing is None or (not existing.label and item.label):
            by[full]=PdfCandidate(full,item.label)

    if not by:
        return None

    rejected_tokens=(
        "supply list","tuition","application","handbook","form","waiver",
    )
    candidates=[]
    for item in by.values():
        haystack=f"{item.label} {item.url}".casefold()
        if any(token in haystack for token in rejected_tokens):
            print(
                "judah-calendar detail: rejected non-calendar PDF candidate"
                + (f" labeled '{item.label}'" if item.label else "")
            )
            continue
        candidates.append(item)

    if not candidates:
        return None

    a,b=_academic_year(reference)
    tokens=(f"{a}-{b}",f"{a}-{str(b)[-2:]}",str(a))

    def score(x):
        h=f"{x.label} {x.url}".casefold()
        return (
            int(x.url.split("?",1)[0] == CURRENT_PDF_FALLBACK),
            int("calendar" in h),
            int(any(t.casefold() in h for t in tokens)),
            int("ugd/" in h),
            -len(x.label) if x.label and "calendar" not in x.label.casefold() else len(x.label),
        )

    return max(candidates,key=score)

def discover_current_pdf(*,reference=None,timeout=25,opener=urlopen):
    reference=reference or date.today()
    try:
        html=_request_bytes(CALENDAR_PAGE,timeout=timeout,accept="text/html,application/xhtml+xml",opener=opener).decode("utf-8","replace")
        c=_discover_pdf_from_html(html,reference=reference)
        if c:
            print("judah-calendar detail: discovered current PDF from Judah calendar page"+(f" labeled '{c.label}'" if c.label else ""))
            return c
    except Exception as exc:
        print(f"judah-calendar detail: calendar-page PDF discovery failed; {type(exc).__name__}: {exc}")
    print("judah-calendar detail: using current known PDF fallback; page discovery will be retried next build")
    return PdfCandidate(CURRENT_PDF_FALLBACK,"2026-2027 Calendar")

def _extract_pdf_text(pdf_bytes):
    if not pdf_bytes.startswith(b"%PDF"):raise RuntimeError("Judah calendar URL did not return a PDF")
    reader=PdfReader(io.BytesIO(pdf_bytes)); pages=[]
    for page in reader.pages:
        try:t=page.extract_text(extraction_mode="layout") or ""
        except TypeError:t=page.extract_text() or ""
        if t.strip():pages.append(t)
    text="\n".join(pages).replace("\u00a0"," ").replace("\u2011","-").replace("\u2012","-").replace("\u2013","-").replace("\u2014","-")
    text=re.sub(r"\n{3,}","\n\n",text)
    if len(text.strip())<100:raise RuntimeError("Judah PDF contained too little extractable text")
    return text


def _parse_calendar_with_fitz(pdf_bytes,*,reference=None,hint=""):
    """
    Second PDF engine for Judah.

    The current 2026-27 PDF's visible month entries are absent from pypdf's
    extracted text layer, even though the headings/footer are readable.
    PyMuPDF exposes positioned words differently, so reconstruct each visible
    month column from word coordinates and feed it through the same month-event
    parser used elsewhere in this collector.
    """
    reference=reference or date.today()

    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required for Judah PDF fallback") from exc

    a,b=_academic_year(reference,hint)
    doc=fitz.open(stream=pdf_bytes,filetype="pdf")
    events=[]

    skip_fragments=(
        "all dates are subject to change",
        "www.judah.org",
        "(217)-359-1701",
        "commit to the lord",
        "proverbs 16:3",
        "note: october 22",
        "judah christian school",
        "1st semester",
        "2nd semester",
    )

    for page_number,page in enumerate(doc, start=1):
        words=page.get_text("words",sort=True) or []
        if not words:
            continue

        # Find month-name words and group headers by approximately equal y.
        month_words=[]
        for w in words:
            token=str(w[4]).strip().casefold()
            if token in MONTHS:
                month_words.append(w)

        if not month_words:
            continue

        y_groups=[]
        for w in sorted(month_words,key=lambda item:(item[1],item[0])):
            y=float(w[1])
            placed=False
            for group in y_groups:
                if abs(group["y"]-y)<=5:
                    group["words"].append(w)
                    group["y"]=(group["y"]*(len(group["words"])-1)+y)/len(group["words"])
                    placed=True
                    break
            if not placed:
                y_groups.append({"y":y,"words":[w]})

        header_groups=[g for g in y_groups if len(g["words"])>=3]
        if not header_groups:
            continue

        # Judah has one five-month header per page. Use the strongest group.
        header=max(header_groups,key=lambda g:len(g["words"]))
        headers=sorted(header["words"],key=lambda w:w[0])
        header_y=max(float(w[3]) for w in headers)

        centers=[(float(w[0])+float(w[2]))/2 for w in headers]
        boundaries=[]
        for i,center in enumerate(centers):
            left=0 if i==0 else (centers[i-1]+center)/2
            right=float(page.rect.width) if i+1==len(centers) else (center+centers[i+1])/2
            boundaries.append((left,right))

        for i,header_word in enumerate(headers):
            month_name=str(header_word[4]).strip().casefold()
            month=MONTHS[month_name]
            year=a if month>=7 else b
            left,right=boundaries[i]

            # Rebuild readable lines from positioned words in this column.
            selected=[]
            for w in words:
                x0,y0,x1,y1,word,block_no,line_no,*_=w
                center=(float(x0)+float(x1))/2
                if not (left<=center<right):
                    continue
                if float(y0)<=header_y+2:
                    continue
                # Keep clear of footer material at the very bottom.
                if float(y0)>float(page.rect.height)-38:
                    continue
                selected.append(w)

            grouped={}
            for w in selected:
                key=(int(w[5]),int(w[6]))
                grouped.setdefault(key,[]).append(w)

            line_records=[]
            for group_words in grouped.values():
                group_words=sorted(group_words,key=lambda w:w[0])
                line=" ".join(str(w[4]) for w in group_words)
                line=re.sub(r"\s+"," ",line).strip()
                y=min(float(w[1]) for w in group_words)
                if not line:
                    continue
                low=line.casefold()
                if any(fragment in low for fragment in skip_fragments):
                    continue
                line_records.append((y,line))

            chunks=[line for _,line in sorted(line_records,key=lambda item:item[0])]
            events.extend(_parse_col(chunks,month,year,b))

    uniq={}
    for e in events:
        key=(
            e["date"],
            e.get("endDate",""),
            re.sub(r"[^a-z0-9]+"," ",e["title"].casefold()).strip(),
        )
        uniq[key]=e

    merged=sorted(
        uniq.values(),
        key=lambda e:(e["date"],e.get("endDate",""),e["title"]),
    )

    if len(merged)<12:
        raise RuntimeError(
            f"PyMuPDF positional extraction produced only {len(merged)} plausible Judah events"
        )

    return merged


def _clean(v):
    v=re.sub(r"\s+"," ",unescape(v)).strip(" |;:")
    return "" if v.casefold() in {"st","nd","rd","th"} else v

def _headers(line):
    m=list(MONTH_RE.finditer(line)); return m if len(m)>=3 else []

def _bounds(matches,width):
    starts=[m.start() for m in matches]; out=[]
    for i,s in enumerate(starts):
        left=0 if i==0 else (starts[i-1]+s)//2
        right=(s+starts[i+1])//2 if i+1<len(starts) else width
        out.append((left,right))
    return out

def _inline_range(title,default_month,default_year,next_year):
    m=INLINE_RANGE_RE.search(title)
    if not m:return title,None,None
    m1,d1=int(m.group("m1")),int(m.group("d1")); m2=int(m.group("m2")) if m.group("m2") else m1; d2=int(m.group("d2"))
    end_year=next_year if default_month>=7 and m2<7 else default_year
    try:s=date(default_year,m1,d1); e=date(end_year,m2,d2)
    except ValueError:return title,None,None
    return _clean(title[:m.start()]+" "+title[m.end():]),s,e

def _event(month,year,next_year,d1,d2,title):
    title=_clean(title)
    if not title or not re.search(r"[A-Za-z]",title):return None
    try:start=date(year,month,d1)
    except ValueError:return None
    finish=None
    if d2:
        try:finish=date(year,month,d2)
        except ValueError:pass
    title,rs,re_=_inline_range(title,month,year,next_year)
    if rs:start=rs
    if re_ and re_>=start:finish=re_
    title=title or "School Event"
    slug=re.sub(r"[^a-z0-9]+","-",title.casefold()).strip("-")[:52] or "event"
    e={"id":f"judah-calendar-{start.isoformat()}-{slug}","title":title,"date":start.isoformat(),"schools":[SCHOOL_ID],"scope":"school","category":"general","source":CALENDAR_SOURCE_NAME,"sourceUrl":CALENDAR_PAGE,"allDay":True}
    if finish and finish>start:e["endDate"]=finish.isoformat()
    return e

def _parse_col(chunks,month,year,next_year):
    out=[]; pending=None
    def flush():
        nonlocal pending
        if pending:
            e=_event(month,year,next_year,pending["d1"],pending.get("d2"),pending["title"])
            if e:out.append(e)
        pending=None
    for raw in chunks:
        c=_clean(raw)
        if not c or c.casefold() in {"1st semester","2nd semester","all dates are subject to change."}:continue
        m=EVENT_START_RE.match(c)
        if m:
            flush(); pending={"d1":int(m.group("d1")),"d2":int(m.group("d2")) if m.group("d2") else None,"title":m.group("title")}
        elif pending:pending["title"]+=" "+c
    flush(); return out

def parse_calendar_text(text,*,reference=None,hint=""):
    reference=reference or date.today(); a,b=_academic_year(reference,hint); lines=text.splitlines()
    headers=[(i,_headers(line)) for i,line in enumerate(lines) if _headers(line)]
    if not headers:raise RuntimeError("Judah PDF extraction exposed no multi-month header row")
    events=[]
    for hp,(idx,matches) in enumerate(headers):
        nxt=headers[hp+1][0] if hp+1<len(headers) else len(lines); section=lines[idx+1:nxt]
        width=max([len(lines[idx])]+[len(x) for x in section]+[1]); bounds=_bounds(matches,width)
        for ci,m in enumerate(matches):
            month=MONTHS[m.group(1).casefold()]; year=a if month>=7 else b; left,right=bounds[ci]
            chunks=[_clean(line[left:right] if left<len(line) else "") for line in section]
            events.extend(_parse_col([x for x in chunks if x],month,year,b))
    uniq={}
    for e in events:uniq[(e["date"],e.get("endDate",""),re.sub(r"[^a-z0-9]+"," ",e["title"].casefold()).strip())]=e
    merged=sorted(uniq.values(),key=lambda e:(e["date"],e.get("endDate",""),e["title"]))
    if len(merged)<12:
        compact=[re.sub(r"\s+"," ",x).strip() for x in lines if re.sub(r"\s+"," ",x).strip()]
        raise RuntimeError(f"Judah PDF text was readable but calendar parsing produced only {len(merged)} plausible events. OPENING PDF TEXT: "+" | ".join(compact[:55])[:3000])
    return merged

def fetch_judah_calendar(*,reference=None,timeout=25,opener=urlopen):
    reference=reference or date.today()
    c=discover_current_pdf(reference=reference,timeout=timeout,opener=opener)
    raw=_request_bytes(
        c.url,
        timeout=timeout,
        accept="application/pdf,*/*;q=0.8",
        opener=opener,
    )
    text=_extract_pdf_text(raw)
    hint=f"{c.label} {c.url} {text[:800]}"

    try:
        events=parse_calendar_text(text,reference=reference,hint=hint)
        engine="pypdf"
    except Exception as pypdf_exc:
        print(
            "judah-calendar detail: pypdf under-read visible calendar entries; "
            f"trying PyMuPDF positional extraction: {type(pypdf_exc).__name__}: {pypdf_exc}"
        )
        try:
            events=_parse_calendar_with_fitz(
                raw,
                reference=reference,
                hint=hint,
            )
            engine="PyMuPDF"
        except Exception as fitz_exc:
            raise RuntimeError(
                "Judah annual PDF could not be parsed by either text engine. "
                f"pypdf: {type(pypdf_exc).__name__}: {pypdf_exc}; "
                f"PyMuPDF: {type(fitz_exc).__name__}: {fitz_exc}"
            ) from fitz_exc

    preview="; ".join(f"{e['date']} {e['title']}" for e in events[:10])
    print(
        f"judah-calendar detail: {engine} parsed {len(events)} school-year events"
    )
    if preview:
        print(f"judah-calendar detail: first parsed events: {preview}")
    return events


DATE_KEYS=("startdatetime","startdate","eventdate","datetime","gamedate","scheduleddate","start")
TITLE_KEYS=("eventname","title","activityname","name","description")
LOCATION_KEYS=("location","facility","facilityname","site","venue")

def _parse_public_datetime(value):
    if isinstance(value,(int,float)):
        if value>1_000_000_000_000:
            try:return datetime.fromtimestamp(value/1000,tz=CHICAGO)
            except:return None
        if value>1_000_000_000:
            try:return datetime.fromtimestamp(value,tz=CHICAGO)
            except:return None
        return None
    clean=str(value or "").strip()
    if not clean:return None
    ms=re.search(r"/Date\((\d+)",clean)
    if ms:
        try:return datetime.fromtimestamp(int(ms.group(1))/1000,tz=CHICAGO)
        except:return None
    norm=clean[:-1]+"+00:00" if clean.endswith("Z") else clean
    try:
        dt=datetime.fromisoformat(norm); dt=dt.replace(tzinfo=CHICAGO) if dt.tzinfo is None else dt
        return dt.astimezone(CHICAGO)
    except:pass
    for fmt in ("%m/%d/%Y %I:%M %p","%m/%d/%Y %I:%M%p","%m/%d/%Y","%Y-%m-%d"):
        try:return datetime.strptime(clean,fmt).replace(tzinfo=CHICAGO)
        except:pass
    return None

def _event_from_arbiter_dict(record,index):
    ci={str(k).casefold():v for k,v in record.items()}
    dt=None
    for k in DATE_KEYS:
        if k in ci:
            dt=_parse_public_datetime(ci[k])
            if dt:
                break
    if not dt:
        return None

    title=""
    for k in TITLE_KEYS:
        v=ci.get(k)
        if isinstance(v,str) and re.search(r"[A-Za-z]",v):
            title=re.sub(r"\s+"," ",v).strip()
            break
    if not title or len(title)>180 or title.casefold() in {"judah christian school","calendar"}:
        return None

    class_name=str(ci.get("classname") or "")
    event_type=""
    match=re.search(r"fc-event-type-([A-Za-z0-9_-]+)",class_name,re.I)
    if match:
        event_type=match.group(1).replace("_"," ").replace("-"," ").strip().title()

    if event_type and event_type.casefold() not in title.casefold():
        title=f"{title} — {event_type}"

    e={
        "id":f"judah-athletics-{dt.date().isoformat()}-{index}",
        "title":title,
        "date":dt.date().isoformat(),
        "schools":[SCHOOL_ID],
        "scope":"school",
        "category":"athletics",
        "source":ATHLETICS_SOURCE_NAME,
        "sourceUrl":ATHLETICS_PAGE,
    }

    if dt.hour or dt.minute:
        e["start"]=dt.strftime("%H:%M")
    else:
        e["allDay"]=True

    end_value=ci.get("end") or ci.get("enddatetime") or ci.get("enddate")
    end_dt=_parse_public_datetime(end_value) if end_value else None
    if end_dt and end_dt.date()==dt.date() and end_dt>dt and (end_dt.hour or end_dt.minute):
        e["end"]=end_dt.strftime("%H:%M")

    for k in LOCATION_KEYS:
        v=ci.get(k)
        if isinstance(v,str) and v.strip():
            e["location"]=re.sub(r"\s+"," ",v).strip()
            break

    relative_url=str(ci.get("url") or "").strip()
    if relative_url:
        e["sourceUrl"]=urljoin("https://arbiterlive.com/",relative_url)

    return e


def _walk(value):
    if isinstance(value,dict):
        yield value
        for v in value.values():yield from _walk(v)
    elif isinstance(value,list):
        for v in value:yield from _walk(v)

def _sanitize(url):
    try:
        p=urlparse(url);return f"{p.scheme}://{p.netloc}{p.path}"
    except:return url.split("?",1)[0]

def _json_shape(value):
    if isinstance(value, dict):
        return {"type":"dict","keys":list(value.keys())[:30]}
    if isinstance(value, list):
        first=value[0] if value else None
        return {
            "type":"list",
            "length":len(value),
            "first":_json_shape(first) if first is not None else None,
        }
    return {"type":type(value).__name__}


def _first_interesting_record(value):
    if isinstance(value, dict):
        if len(value) >= 4:
            return value
        for child in value.values():
            found=_first_interesting_record(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found=_first_interesting_record(child)
            if found is not None:
                return found
    return None


def fetch_judah_athletics(*,reference=None,settle_seconds=2.5):
    reference=reference or date.today()

    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.chrome.options import Options
    except ImportError as exc:
        raise RuntimeError("Selenium is required for Judah athletics") from exc

    o=Options()
    o.add_argument("--headless=new")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-dev-shm-usage")
    o.add_argument("--window-size=1440,1200")
    o.add_argument("--lang=en-US")
    o.page_load_strategy="eager"

    d=webdriver.Chrome(options=o)
    d.set_page_load_timeout(18)
    d.set_script_timeout(25)

    try:
        try:
            d.get(ATHLETICS_PAGE)
        except TimeoutException:
            try:
                d.execute_script("window.stop();")
            except Exception:
                pass

        time.sleep(settle_seconds)

        start_day=reference.fromordinal(reference.toordinal()-30)
        end_day=reference.fromordinal(reference.toordinal()+60)

        script=r"""
        const startDate=arguments[0];
        const endDate=arguments[1];
        const done=arguments[arguments.length-1];

        const body=new URLSearchParams();
        body.set('startDate',startDate);
        body.set('endDate',endDate);

        fetch('/School/GetEventsByEntity/',{
          method:'POST',
          credentials:'same-origin',
          headers:{
            'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With':'XMLHttpRequest'
          },
          body:body.toString()
        })
        .then(async response=>{
          const text=await response.text();
          done({ok:response.ok,status:response.status,text:text});
        })
        .catch(error=>done({ok:false,status:0,text:String(error)}));
        """

        result=d.execute_async_script(
            script,
            start_day.isoformat(),
            end_day.isoformat(),
        ) or {}

        if not result.get("ok"):
            raise RuntimeError(
                f"Arbiter rolling-window request failed with status "
                f"{result.get('status')}: {str(result.get('text') or '')[:300]}"
            )

        try:
            payload=json.loads(result.get("text") or "{}")
        except Exception as exc:
            raise RuntimeError("Arbiter response was not valid JSON") from exc

        detail_string=payload.get("EventsFilteredDetailString")
        if not isinstance(detail_string,str) or not detail_string.strip():
            raise RuntimeError("Arbiter response omitted EventsFilteredDetailString")

        try:
            detail_events=json.loads(detail_string)
        except Exception as exc:
            raise RuntimeError(
                "Arbiter EventsFilteredDetailString was not valid nested JSON"
            ) from exc

        if not isinstance(detail_events,list):
            raise RuntimeError("Arbiter detail payload was not an event list")

        parsed=[]
        for i,record in enumerate(detail_events, start=1):
            if not isinstance(record,dict):
                continue
            event=_event_from_arbiter_dict(record,i)
            if event:
                parsed.append(event)

        uniq={}
        for e in parsed:
            key=(
                e["date"],
                e.get("start",""),
                e.get("end",""),
                re.sub(r"[^a-z0-9]+"," ",e["title"].casefold()).strip(),
            )
            uniq[key]=e

        events=sorted(
            uniq.values(),
            key=lambda e:(e["date"],e.get("start",""),e["title"]),
        )

        if len(events)<3:
            raise RuntimeError(
                f"Judah Arbiter returned {len(detail_events)} detail records but "
                f"only {len(events)} safely parseable events"
            )

        preview="; ".join(
            f"{e['date']} {e['title']}"
            for e in events[:10]
        )
        print(
            f"judah-athletics detail: Arbiter rolling window "
            f"{start_day.isoformat()} through {end_day.isoformat()}; "
            f"parsed {len(events)} unique events"
        )
        if preview:
            print(f"judah-athletics detail: first parsed events: {preview}")

        return events

    finally:
        d.quit()

