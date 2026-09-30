// 把文件列表写进剪贴板，并记账供 clipwatch.js 防回环。
// 调用：osascript -l JavaScript clipwrite.js '["/路径/a.txt","/路径/b.pdf"]'
// 输出：{"ok":true,"cc":107,"count":2}

ObjC.import('AppKit');
ObjC.import('Foundation');

function run(argv) {
  var paths = JSON.parse(argv[0]);
  var STATE = ObjC.unwrap($.NSHomeDirectory()) + '/AgentBridge/.clip_self.json';

  function writeState(o) {
    var s = $.NSString.alloc.initWithUTF8String(JSON.stringify(o));
    s.writeToFileAtomicallyEncodingError(STATE, true, $.NSUTF8StringEncoding, $());
  }

  var pb = $.NSPasteboard.generalPasteboard;
  var before = Number(pb.changeCount);
  writeState({ pending: true, before: before, src: 'clipwrite' });

  var arr = $.NSMutableArray.array;
  for (var i = 0; i < paths.length; i++) arr.addObject($.NSURL.fileURLWithPath(paths[i]));

  pb.clearContents;
  var ok = pb.writeObjects(arr);
  var cc = Number(pb.changeCount);
  writeState({ pending: false, before: before, cc: cc, src: 'clipwrite' });

  return JSON.stringify({ ok: !!ok, cc: cc, count: paths.length });
}
