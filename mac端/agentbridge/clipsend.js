// 把命令行给的路径写进剪贴板，但【不】写 .clip_self.json 记账。
// 用途：模拟用户在 Finder 里复制文件，触发 clipwatch 发送（联调/自测用）。
// 调用：osascript -l JavaScript clipsend.js '["/路径/a","/路径/b"]'

ObjC.import('AppKit');
ObjC.import('Foundation');

function run(argv) {
  var paths = JSON.parse(argv[0]);
  var pb = $.NSPasteboard.generalPasteboard;
  var arr = $.NSMutableArray.array;
  for (var i = 0; i < paths.length; i++) arr.addObject($.NSURL.fileURLWithPath(paths[i]));
  pb.clearContents;
  var ok = pb.writeObjects(arr);
  return JSON.stringify({ ok: !!ok, cc: Number(pb.changeCount), count: paths.length, paths: paths });
}
