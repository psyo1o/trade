' CMD 창 없이 GUI + 자동 재시작 (launch_gui.py --no-console)
' 바로가기 만들 때 대상을 이 파일로 지정하면 됩니다.

Set fso = CreateObject("Scripting.FileSystemObject")
botDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = botDir
sh.Run "py -3.11 launch_gui.py --no-console", 0, False
