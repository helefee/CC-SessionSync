; CC Session Sync Windows installer - Inno Setup 6.5+  (structure ported from CC Msg's installer\CCMsg.iss)
; Build: tools\build_desktop.ps1 calls  ISCC /DAppVersion=x.y.z /DSourceDir=<dist\CCSessionSync> /DOutputDir=<dist> installer\CCSessionSync.iss
; - per-user install to %LOCALAPPDATA%\Programs\CCSessionSync ({userpf}), no admin / UAC
; - Start menu shortcut, optional desktop shortcut, uninstaller registered under the current user
; - same AppId => upgrades in place; a running copy (maybe hidden in the tray) is asked to quit first (CCSessionSync.exe --quit)
; - files still in use (an after-quit helper waiting for Claude / Codex to exit) are renamed out of the way instead of blocking
; - silent update from the app (only after the user clicked 立即更新):
;     Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS /NORESTART /RELAUNCH=1 [/RELAUNCHARGS="--port N"]
; - user data (~\.cc-sessionsync) is never touched by install / uninstall (except the small update-rollback.json note below)
; - rollback (0.4.2): on an upgrade the installed version is moved to {app}.prev first; after the new files are in place
;   "CCSessionSync.exe health" (headless start-up check) runs; if it fails the previous version is moved back, the
;   uninstall entry is set back to the old version, a notification is shown and ~\.cc-sessionsync\update-rollback.json is written.
;   /FAILHEALTH=1 simulates a failing health check (CI test of the rollback path).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\CCSessionSync"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
; /DTestBuild=1 builds an isolated "CC Session Sync TEST" (own AppId, folder, mutex, tray, autostart value) for tests
#ifdef TestBuild
  #define MyGuid "3B8E2D41-5C7A-4E19-A0F2-7E57C5513C0D"
  #define MyAppId "{{" + MyGuid + "}"
  #define MyName "CC Session Sync TEST"
  #define MyDir "CCSessionSyncTEST"
  #define MyMutex "Local\CCSessionSyncDesktop-TEST"
  #define MyRun "CC Session Sync TEST"
  #define MyOut "CCSessionSync-TEST-Setup-"
#else
  #define MyGuid "9A4C7E15-2B3D-4F68-8C1E-5D2A9B7E3F41"
  #define MyAppId "{{" + MyGuid + "}"
  #define MyName "CC Session Sync"
  #define MyDir "CCSessionSync"
  #define MyMutex "Local\CCSessionSyncDesktop"
  #define MyRun "CC Session Sync"
  #define MyOut "CCSessionSync-Setup-"
#endif

[Setup]
AppId={#MyAppId}
AppName={#MyName}
AppVersion={#AppVersion}
AppVerName={#MyName} {#AppVersion}
AppPublisher=helefee
AppPublisherURL=https://github.com/helefee/CC-SessionSync
AppSupportURL=https://github.com/helefee/CC-SessionSync/issues
AppUpdatesURL=https://github.com/helefee/CC-SessionSync/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={userpf}\{#MyDir}
DefaultGroupName={#MyName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename={#MyOut}{#AppVersion}
SetupIconFile=..\cc_sessionsync\icon.ico
UninstallDisplayIcon={app}\CCSessionSync.exe
UninstallDisplayName={#MyName} {#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMADictionarySize=131072
LZMANumFastBytes=273
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=CCSessionSync.exe
RestartApplications=no
SetupMutex=CCSessionSyncSetupMutex{#MyDir}
ShowLanguageDialog=no
LanguageDetectionMethod=none

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl,CCSessionSync.zh.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{cm:AppTitle}"; Filename: "{app}\CCSessionSync.exe"; WorkingDir: "{app}"
Name: "{autoprograms}\{cm:Uninstall}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{cm:AppTitle}"; Filename: "{app}\CCSessionSync.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\CCSessionSync.exe"; Description: "{cm:LaunchNow}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\CCSessionSync.exe"; Parameters: "{param:RELAUNCHARGS}"; Flags: nowait; Check: ShouldRelaunch

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{app}.prev"

[Code]
var
  HavePrev, RolledBack: Boolean;
  PrevVer, HealthMsg: String;

function ShouldRelaunch: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;

{ wait up to ~20 s for a running CC Session Sync window (it holds this mutex) to quit by itself }
procedure WaitForApp;
var
  i: Integer;
begin
  i := 0;
  while CheckForMutexes('{#MyMutex}') and (i < 80) do
  begin
    Sleep(250);
    i := i + 1;
  end;
end;

{ move every old program file out of the way: delete it, or if it is in use rename it
  (Windows allows renaming a running exe / loaded dll); leftovers go on the next upgrade / uninstall }
procedure ClearDir(const Dir: String);
var
  FR: TFindRec;
  P: String;
begin
  if FindFirst(Dir + '\*', FR) then
  try
    repeat
      if (FR.Name <> '.') and (FR.Name <> '..') then
      begin
        P := Dir + '\' + FR.Name;
        if FR.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0 then
          ClearDir(P)
        else if (CompareText(Copy(FR.Name, 1, 5), 'unins') <> 0) then
        begin
          if not DeleteFile(P) then
            RenameFile(P, P + '.old-' + GetDateTimeString('yyyymmddhhnnsszzz', #0, #0));
        end;
      end;
    until not FindNext(FR);
  finally
    FindClose(FR);
  end;
  RemoveDir(Dir);
end;

{ the app may be hidden in the tray (closing the window only hides it): CCSessionSync.exe --quit posts a quit
  message to its tray window and waits for its mutex to go away }
procedure QuitRunningApp;
var
  rc: Integer;
begin
  if CheckForMutexes('{#MyMutex}') and FileExists(ExpandConstant('{app}\CCSessionSync.exe')) then
    Exec(ExpandConstant('{app}\CCSessionSync.exe'), '--quit', '', SW_HIDE, ewWaitUntilTerminated, rc);
end;

function PrevDir: String;
begin
  Result := ExpandConstant('{app}') + '.prev';
end;

{ move every file (except the uninstaller) from Src into Dst, keeping the tree; a file that cannot be moved is copied }
procedure MoveTree(const Src, Dst: String);
var
  FR: TFindRec;
  P, Q: String;
begin
  ForceDirectories(Dst);
  if FindFirst(Src + '\*', FR) then
  try
    repeat
      if (FR.Name <> '.') and (FR.Name <> '..') then
      begin
        P := Src + '\' + FR.Name;
        Q := Dst + '\' + FR.Name;
        if FR.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0 then
        begin
          MoveTree(P, Q);
          RemoveDir(P);
        end
        else if (CompareText(Copy(FR.Name, 1, 5), 'unins') <> 0) then
        begin
          if not RenameFile(P, Q) then
            FileCopy(P, Q, False);
        end;
      end;
    until not FindNext(FR);
  finally
    FindClose(FR);
  end;
end;

function DataDir: String;
begin
  Result := GetEnv('CC_SESSIONSYNC_HOME');
  if Result = '' then
    Result := ExpandConstant('{%USERPROFILE}') + '\.cc-sessionsync';
end;

function JsonStr(const S: String): String;
begin
  Result := S;
  StringChangeEx(Result, '\', '\\', True);
  StringChangeEx(Result, '"', '\"', True);
  StringChangeEx(Result, #13, ' ', True);
  StringChangeEx(Result, #10, ' ', True);
  StringChangeEx(Result, #9, ' ', True);
end;

procedure SetUninstallVersion(const V: String);
var
  K: String;
begin
  if V = '' then
    exit;
  K := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{' + '{#MyGuid}' + '}_is1';
  if RegKeyExists(HKEY_CURRENT_USER, K) then
  begin
    RegWriteStringValue(HKEY_CURRENT_USER, K, 'DisplayVersion', V);
    RegWriteStringValue(HKEY_CURRENT_USER, K, 'DisplayName', '{#MyName} ' + V);
  end;
end;

procedure Notify(const Msg: String);
var
  rc: Integer;
  Cmd: String;
begin
  if not WizardSilent then
  begin
    MsgBox(Msg, mbError, MB_OK);
    exit;
  end;
  { silent in-app update: a tray balloon via PowerShell (works whatever version was restored) }
  Cmd := '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.Windows.Forms;' +
         ' $n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon(''' +
         ExpandConstant('{app}\CCSessionSync.exe') + '''); $n.Visible = $true; $n.ShowBalloonTip(15000, ''{#MyName}'', ''' + Msg +
         ''', ''Warning''); Start-Sleep -Seconds 16; $n.Dispose()"';
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Cmd, '', SW_HIDE, ewNoWait, rc);
end;

function NowStr: String;
var
  S: String;
begin
  { local time, ISO-like; the app reads it back as a local timestamp }
  S := GetDateTimeString('yyyy/mm/dd', '-', ':') + 'T' + GetDateTimeString('hh:nn:ss', '-', ':');
  Result := S;
end;

{ run the new version's headless start-up check; on failure put the previous version back }
procedure HealthCheckOrRollback;
var
  rc: Integer;
  Args, OutF, Rec: String;
  Ok: Boolean;
  Res: AnsiString;
begin
  OutF := ExpandConstant('{tmp}\health.json');
  Args := 'health --timeout 45 --out "' + OutF + '"';
  if ExpandConstant('{param:FAILHEALTH|0}') = '1' then
    Args := Args + ' --fail';
  Ok := Exec(ExpandConstant('{app}\CCSessionSync.exe'), Args, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, rc) and (rc = 0);
  if Ok then
  begin
    DelTree(PrevDir, True, True, True);
    exit;
  end;
  HealthMsg := 'exit ' + IntToStr(rc);
  if LoadStringFromFile(OutF, Res) then
    HealthMsg := HealthMsg + ' ' + Copy(String(Res), 1, 600);
  { put the previous version back }
  ClearDir(ExpandConstant('{app}'));
  MoveTree(PrevDir, ExpandConstant('{app}'));
  DelTree(PrevDir, True, True, True);
  RolledBack := True;
  SetUninstallVersion(PrevVer);
  ForceDirectories(DataDir);
  Rec := '{"from": "' + JsonStr(PrevVer) + '", "to": "' + JsonStr('{#AppVersion}') + '", "when": "' + NowStr +
         '", "reason": "' + JsonStr(HealthMsg) + '"}';
  SaveStringToFile(DataDir + '\update-rollback.json', Rec, False);
  Notify(FmtMessage(CustomMessage('RolledBack'), ['{#AppVersion}', PrevVer]));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    QuitRunningApp;
    WaitForApp;
    HavePrev := False;
    RolledBack := False;
    if FileExists(ExpandConstant('{app}\CCSessionSync.exe')) then
    begin
      { keep the version that is installed now, so a broken update can be undone }
      if not GetVersionNumbersString(ExpandConstant('{app}\CCSessionSync.exe'), PrevVer) then
        PrevVer := '';
      if (Length(PrevVer) > 2) and (Copy(PrevVer, Length(PrevVer) - 1, 2) = '.0') then
        PrevVer := Copy(PrevVer, 1, Length(PrevVer) - 2);
      DelTree(PrevDir, True, True, True);
      MoveTree(ExpandConstant('{app}'), PrevDir);
      HavePrev := FileExists(PrevDir + '\CCSessionSync.exe');
    end;
    if DirExists(ExpandConstant('{app}')) then
      ClearDir(ExpandConstant('{app}'));
  end;
  if (CurStep = ssPostInstall) and HavePrev then
    HealthCheckOrRollback;
  if (CurStep = ssDone) and RolledBack then
    SetUninstallVersion(PrevVer);
#ifdef TestBuild
  if CurStep = ssPostInstall then
    SaveStringToFile(ExpandConstant('{app}\instance.txt'), 'TEST', False);
#endif
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    QuitRunningApp;
    WaitForApp;
    { autostart entry written by the app (tray menu / settings): remove it with the app }
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#MyRun}');
    { 0.3.0-0.3.4 model-routing proxy autostart (feature removed in 0.3.5; the app also removes it on first launch) }
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'CC Session Sync Route');
  end;
end;
