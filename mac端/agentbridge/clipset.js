// 把文本或图片写进 NSPasteboard，并记账供 clipwatch.js 防回环。
// 调用：osascript -l JavaScript clipset.js '{"kind":"text","text":"...","fp":"text:xxx"}'
//       osascript -l JavaScript clipset.js '{"kind":"image","path":"/tmp/x.png","fp":"image:xxx"}'
// 输出：{"ok":true,"cc":130,"kind":"text"}
// fp 由调用方（Python）算好传入，作为「内容指纹」第二层防回环（协议 §4）。

ObjC.import('AppKit');
ObjC.import('Foundation');

var PB_TEXT = 'public.utf8-plain-text';
var PB_PNG = 'public.png';

function statePath() {
  return ObjC.unwrap($.NSHomeDirectory()) + '/AgentBridge/.clip_self.json';
}

function writeState(o) {
  var s = $.NSString.alloc.initWithUTF8String(JSON.stringify(o));
  s.writeToFileAtomicallyEncodingError(statePath(), true, $.NSUTF8StringEncoding, $());
}

function run(argv) {
  var req = JSON.parse(argv[0]);
  var pb = $.NSPasteboard.generalPasteboard;
  var before = Number(pb.changeCount);
  writeState({ pending: true, before: before, kind: req.kind, fp: req.fp, src: 'clipset' });

  var ok = false;
  var err = null;
  try {
    pb.clearContents;
    if (req.kind === 'text') {
      pb.declareTypesOwner($.NSArray.arrayWithObject(PB_TEXT), $());
      ok = pb.setStringForType($(req.text), PB_TEXT);
    } else {
      var d = $.NSData.dataWithContentsOfFile($(req.path));
      if (!d || d.isNil()) throw new Error('读不到图片文件: ' + req.path);
      pb.declareTypesOwner($.NSArray.arrayWithObject(PB_PNG), $());
      ok = pb.setDataForType(d, PB_PNG);
    }
  } catch (e) {
    err = String(e);
  }

  var cc = Number(pb.changeCount);
  writeState({ pending: false, cc: cc, before: before, kind: req.kind,
               fp: ok ? req.fp : '', src: 'clipset', err: err || '' });
  var out = { ok: !!ok, cc: cc, kind: req.kind };
  if (err) out.err = err;
  return JSON.stringify(out);
}
