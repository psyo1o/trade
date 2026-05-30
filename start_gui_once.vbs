' CMD 없음, 자동 재시작 없음 (GUI 1회만). 끌 때는 창 X 로 종료.
Set fso = CreateObject("Scripting.FileSystemObject")
botDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = botDir
sh.Run "py -3.11 launch_gui.py --no-console --once", 0, False
