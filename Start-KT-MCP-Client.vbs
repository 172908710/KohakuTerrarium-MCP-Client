Option Explicit

Dim shell, fso, root, pythonw, command, message
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = root & "\runtime\pythonw.exe"

If Not fso.FileExists(pythonw) Then
    message = "Bundled Python is missing." & vbCrLf & "Extract the complete portable ZIP, including the runtime folder."
    MsgBox message, 16, "KT MCP Client"
    WScript.Quit 1
End If
If Not fso.FileExists(root & "\launch.py") Then
    MsgBox "launch.py is missing. Extract the complete portable ZIP.", 16, "KT MCP Client"
    WScript.Quit 1
End If

shell.CurrentDirectory = root
command = """" & pythonw & """ -I -X utf8 """ & root & "\launch.py"""
shell.Run command, 0, False
