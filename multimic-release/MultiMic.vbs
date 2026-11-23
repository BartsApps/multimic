Option Explicit
Dim sh, fso, dir, app, exe
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
exe = dir & "\dist\MultiMic.exe"
app = dir & "\multimic_app.py"
If fso.FileExists(exe) Then
    sh.Run """" & exe & """", 1, False
    WScript.Quit
End If
On Error Resume Next
sh.Run "pyw """ & app & """", 1, False
If Err.Number <> 0 Then
    Err.Clear
    sh.Run "pythonw """ & app & """", 1, False
    If Err.Number <> 0 Then
        MsgBox "Python was not found. Install it from python.org (tick 'Add python.exe to PATH')," & vbCrLf & _
               "then run:  py -m pip install -r requirements.txt", 48, "MultiMic"
    End If
End If
