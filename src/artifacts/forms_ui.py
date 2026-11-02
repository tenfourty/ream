"""The gallery's half of answering a form (docs/agent/specs/2026-10-29-form-controls-design.md
3.3): the data block the page carries, `/forms.js` (the Submit bar and the summary sheet)
and the route that takes the batch. Kept out of gallery.py, which registers it with one line.

The gallery draws everything the reader confirms. The page's own controls only report values
(content.py FORM_JS); the labels, options and limits shown here and checked at submit come from
the field list stored at publish, never from the page.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from artifacts import formanswer, forms, store
from artifacts.content import _script_json

_STYLE = (
    "<style>"
    "#artifact-forms-bar{position:fixed;left:0;right:0;bottom:0;z-index:30;display:flex;"
    "flex-direction:column;gap:1px}"
    "#artifact-forms-bar .fbar{display:flex;align-items:center;gap:.8rem;padding:.55rem 1rem;"
    "background:var(--surface,#fff);color:var(--text,#111);border-top:1px solid rgba(127,127,127,.35);"
    "font:.9rem/1.3 system-ui,sans-serif}"
    "#artifact-forms-bar .ftitle{font-weight:600;flex:0 1 auto;overflow:hidden;text-overflow:ellipsis;"
    "white-space:nowrap}"
    "#artifact-forms-bar .fcount{flex:1 1 auto;opacity:.75}"
    "#artifact-forms-bar button,.fsheet button{font:inherit;padding:.35rem .9rem;border-radius:.4rem;"
    "border:1px solid rgba(127,127,127,.5);background:transparent;color:inherit;cursor:pointer}"
    "#artifact-forms-bar button.fgo,.fsheet button.fgo{background:#2a6df4;border-color:#2a6df4;color:#fff}"
    "#artifact-forms-bar button[disabled],.fsheet button[disabled]{opacity:.45;cursor:not-allowed}"
    ".fscrim{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:40;display:flex;"
    "align-items:center;justify-content:center;padding:1rem}"
    ".fsheet{background:var(--surface,#fff);color:var(--text,#111);max-width:42rem;width:100%;"
    "max-height:85vh;overflow:auto;border-radius:.6rem;padding:1.2rem;font:.95rem/1.4 system-ui,sans-serif}"
    ".fsheet h2{margin:0 0 .8rem;font-size:1.1rem}"
    ".fsheet dl{margin:0 0 1rem}.fsheet dt{font-weight:600;margin-top:.6rem}"
    ".fsheet dd{margin:.1rem 0 0;white-space:pre-wrap;overflow-wrap:anywhere}"
    ".fsheet .fblank{opacity:.6;font-style:italic}"
    ".fsheet .ferr{color:#c0392b;margin:0 0 .8rem}"
    ".fsheet .fbtns{display:flex;gap:.6rem;justify-content:flex-end}"
    "body.fhas{padding-bottom:4.5rem}"
    "</style>"
)


def _answer_view(record: dict | None) -> dict | None:
    if not record:
        return None
    return {
        "answered_at": record.get("answered_at"),
        "answered_by": record.get("answered_by"),
        "version": record.get("version"),
    }


def block(db, artifact_id: str) -> str:
    """The `<style>`, mount point, JSON data block and script tag for a page's open
    forms; empty when the page has none. The data is the STORED field list at each
    form's latest version, which the script trusts over anything the page says."""
    shown = forms.for_page(db, artifact_id)
    if not shown:
        return ""
    with db.connect() as conn:
        records = {
            did: rec
            for did, rec in conn.execute(
                "select id, answer_record from decision"
                " where artifact_id = %s and kind = 'form'",
                (artifact_id,),
            ).fetchall()
        }
    payload = {
        "artifact_id": artifact_id,
        "forms": [
            {
                "form": f["form"],
                "title": f["title"],
                "version": f["version"],
                "complete": f["complete"],
                "fields": f["fields"],
                "answered": _answer_view(records.get(f["decision_id"])),
            }
            for f in shown
        ],
    }
    return (
        _STYLE
        + '<div id="artifact-forms-bar"></div>'
        + '<script type="application/json" id="artifact-forms">'
        + _script_json(payload)
        + "</script>"
        + '<script src="/forms.js" defer></script>'
    )


_MAX_BODY = 1_000_000


def register(
    router: APIRouter,
    db,
    *,
    same_origin: Callable[[Request], None],
    headers: Callable[[], dict[str, str]],
) -> None:
    @router.get("/forms.js")
    def forms_script() -> Response:
        return Response(
            FORMS_JS,
            media_type="text/javascript; charset=utf-8",
            headers={**headers(), "cache-control": "no-cache"},
        )

    @router.post("/a/{artifact_id}/forms/{form}")
    async def submit_form(artifact_id: str, form: str, request: Request):
        same_origin(request)
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HTTPException(415, "send application/json")
        # Cap the body before parsing: a form's values are small, and a client
        # that streams without a Content-Length is cut off as it reads.
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > _MAX_BODY:
            raise HTTPException(413, "the submission is too large")
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > _MAX_BODY:
                raise HTTPException(413, "the submission is too large")
        try:
            body = json.loads(raw)
        except ValueError:
            raise HTTPException(400, "not JSON") from None
        version = body.get("version") if isinstance(body, dict) else None
        if (
            not isinstance(body, dict)
            or isinstance(version, bool)
            or not isinstance(version, int)
        ):
            return JSONResponse(
                {"detail": "send {version, values}", "field": None}, status_code=422
            )

        def work():
            with db.connect() as conn:
                row = conn.execute(
                    "select id from decision where artifact_id = %s and kind = 'form'"
                    " and form_name = %s and closed_at is null",
                    (artifact_id, form),
                ).fetchone()
            if row is None:
                raise store.NotFound()
            return formanswer.submit(
                db, row[0], version=version, values=body.get("values")
            )

        try:
            record = await run_in_threadpool(work)
        except formanswer.SubmitRefused as exc:
            return JSONResponse(
                {"detail": exc.detail, "field": exc.field}, status_code=exc.status
            )
        except store.NotFound:
            raise HTTPException(404, "no such form") from None
        except store.ScanFailed:
            raise HTTPException(
                503, "the secret check could not run; nothing was stored"
            ) from None
        total = len(record["answers"])
        return JSONResponse(
            {
                "ok": True,
                "answered": total - len(record["blank"]),
                "total": total,
                "blank": record["blank"],
                "answered_at": record["answered_at"],
            }
        )


FORMS_JS = r"""(function(){
"use strict";
var data=null,frame=document.getElementById("artifact-frame"),bar=document.getElementById("artifact-forms-bar");
try{data=JSON.parse(document.getElementById("artifact-forms").textContent)}catch(e){}
if(!data||!frame||!bar||!Array.isArray(data.forms)||!data.forms.length)return;
var origin;try{origin=new URL(frame.getAttribute("src"),location.href).origin}catch(e){return}
var FMX=200,FMV=10000,by=Object.create(null);
document.body.classList.add("fhas");
function key(f){return"artifact-form:"+data.artifact_id+":"+f.form}
function isBlank(spec,v){
  if(spec.type==="checkbox"&&!(spec.options&&spec.options.length))return false;
  if(spec.type==="checkbox")return!Array.isArray(v)||!v.length;
  if(v===null||v===undefined)return true;
  if(typeof v==="string")return!v.trim();
  return false}
// Only the stored field names survive, with the same caps the page side applies.
function clean(f,raw){var out=Object.create(null),n=0;
  f.fields.forEach(function(spec){
    if(!Object.prototype.hasOwnProperty.call(raw,spec.name)||n>=FMX)return;
    var v=raw[spec.name];n++;
    if(typeof v==="string")v=v.slice(0,FMV);
    else if(Array.isArray(v))v=v.slice(0,FMX).filter(function(x){return typeof x==="string"}).map(function(x){return x.slice(0,FMV)});
    else if(typeof v!=="boolean"&&typeof v!=="number"&&v!==null)return;
    out[spec.name]=v});
  var o={};for(var k in out)o[k]=out[k];return o}
function load(f){try{var s=sessionStorage.getItem(key(f));if(!s)return null;
  var d=JSON.parse(s);if(!d||typeof d!=="object")return null;
  d=clean(f,d);return Object.keys(d).length?d:null}catch(e){return null}}
function save(f){try{sessionStorage.setItem(key(f),JSON.stringify(f.values))}catch(e){}}
function value(f,spec){return f.values?f.values[spec.name]:undefined}
function counts(f){var done=0,missing=false;
  f.fields.forEach(function(spec){
    if(isBlank(spec,value(f,spec))){if(spec.required||f.complete==="all")missing=true}else done++});
  return{done:done,total:f.fields.length,missing:missing}}
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;
  if(text!==undefined)e.textContent=text;return e}
function refresh(f){var c=counts(f);
  f.count.textContent=c.done+" of "+c.total+" answered"+(f.answered?" · last sent "+when(f.answered.answered_at):"");
  f.go.disabled=!f.values||c.missing;
  f.go.title=c.missing?(f.complete==="all"?"Every field needs an answer":"A required field is blank"):""}
function when(iso){try{return new Date(iso).toLocaleString([],{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"})}catch(e){return""}}
function optLabel(spec,v){var o=(spec.options||[]).filter(function(x){return x.value===v})[0];
  return o?o.label:String(v)}
function shown(spec,v){
  if(isBlank(spec,v))return null;
  if(spec.type==="checkbox"&&!(spec.options&&spec.options.length))return v?"Yes":"No";
  if(Array.isArray(v))return v.map(function(x){return optLabel(spec,x)}).join(", ");
  if(spec.type==="radio"||spec.type==="select")return optLabel(spec,v);
  return String(v)}
function body(f){var out=Object.create(null);
  f.fields.forEach(function(spec){var v=value(f,spec);
    if(spec.type==="radio"&&(v===undefined||v===""))v=null;
    if(v===undefined)return;
    if(Array.isArray(v)&&!v.length)return;
    if(typeof v==="string"&&!v.trim())return;
    if(v===null)return;out[spec.name]=v});
  var o={};for(var k in out)o[k]=out[k];return o}
function sheet(f){
  // What the sheet shows is exactly what Confirm posts: one frozen copy, taken now. A
  // later report from the page cannot swap it (see the message handler, which closes
  // the sheet if the values move while it is open).
  var sent=body(f),scrim=el("div","fscrim"),box=el("div","fsheet"),h=el("h2","",f.title+" — send these answers?"),
    dl=el("dl"),err=el("p","ferr"),btns=el("div","fbtns"),no=el("button","","Back"),go=el("button","fgo","Confirm and send");
  box.setAttribute("role","dialog");box.setAttribute("aria-modal","true");err.hidden=true;
  f.fields.forEach(function(spec){var dt=el("dt","",spec.label||spec.name),dd,s=shown(spec,sent[spec.name]);
    dd=s===null?el("dd","fblank","(blank)"):el("dd","",s);dl.appendChild(dt);dl.appendChild(dd)});
  no.type="button";go.type="button";btns.appendChild(no);btns.appendChild(go);
  [h,dl,err,btns].forEach(function(n){box.appendChild(n)});scrim.appendChild(box);
  function close(){scrim.remove();document.removeEventListener("keydown",esc,true);
    if(f.closeSheet===close)f.closeSheet=null}
  f.closeSheet=close;
  function esc(e){if(e.key==="Escape"){e.preventDefault();close()}}
  document.addEventListener("keydown",esc,true);
  no.addEventListener("click",close);
  scrim.addEventListener("click",function(e){if(e.target===scrim)close()});
  go.addEventListener("click",function(){go.disabled=true;err.hidden=true;
    fetch("/a/"+encodeURIComponent(data.artifact_id)+"/forms/"+encodeURIComponent(f.form),{
      method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","Accept":"application/json"},
      body:JSON.stringify({version:f.version,values:sent})})
    .then(function(r){return r.json().catch(function(){return{}}).then(function(j){return{r:r,j:j}})})
    .then(function(x){
      if(x.r.ok){f.answered={answered_at:x.j.answered_at};close();refresh(f);return}
      var lab="";f.fields.forEach(function(s){if(s.name===x.j.field)lab=s.label||s.name});
      err.textContent=(lab?lab+": ":"")+(x.j.detail||("It was not sent ("+x.r.status+")"));
      err.hidden=false;go.disabled=false})
    .catch(function(){err.textContent="It was not sent: the gallery could not be reached.";
      err.hidden=false;go.disabled=false})});
  document.body.appendChild(scrim);go.focus()}
data.forms.forEach(function(f){
  f.values=null;f.awaiting=false;by[f.form]=f;
  var row=el("div","fbar"),t=el("span","ftitle",f.title);
  f.count=el("span","fcount");f.go=el("button","fgo","Submit");f.go.type="button";
  f.go.addEventListener("click",function(){if(!f.go.disabled)sheet(f)});
  row.appendChild(t);row.appendChild(f.count);row.appendChild(f.go);bar.appendChild(row);refresh(f)});
addEventListener("message",function(e){
  if(e.source!==frame.contentWindow||e.origin!==origin)return;
  var d=e.data;
  if(!d||typeof d!=="object"||d.type!=="artifact-form"||typeof d.form!=="string")return;
  if(!d.values||typeof d.values!=="object")return;
  var f=by[d.form];if(!f)return;
  if(d.load===true){var draft=load(f);
    if(draft){f.awaiting=true;frame.contentWindow.postMessage({type:"artifact-form-fill",form:f.form,values:draft},origin);return}}
  f.awaiting=false;
  var before=f.closeSheet?JSON.stringify(body(f)):null;
  f.values=clean(f,d.values);save(f);
  // The sheet is open on an earlier set of values: close it rather than let it say one
  // thing and send another; he reopens it to review the new ones.
  if(f.closeSheet&&JSON.stringify(body(f))!==before)f.closeSheet();
  refresh(f)});
})();
"""
