; Network Guard 安装包脚本（Inno Setup 6）
;
; 由 src/build.py 调用：
;   ISCC.exe /DMyAppVersion=<版本> /DSourceRoot=<dist\NetworkGuard 绝对路径> installer.iss
;
; 设计要点：
; 1. PrivilegesRequired=lowest：按“当前用户”安装到 %LOCALAPPDATA%\Programs\NetworkGuard，
;    不触发 UAC；目录页仍可让用户改成任意可写路径（D:\Tools\NetworkGuard 等）。
; 2. 安装完成后自动清除 Windows 下载标记（Mark of the Web），
;    这样安装目录里的 .bat / .exe 不会再弹「无法验证发布者」。
; 3. 卸载时只删除程序目录；用户数据（%LOCALAPPDATA%\NetworkGuard 下的配置与日志）保留。

#define MyAppName "Network Guard"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef SourceRoot
  #define SourceRoot "..\dist\NetworkGuard"
#endif
; Inno 的文件版本号必须是纯数字（主.次.修订.构建），由 build.py 换算后传入
#ifndef MyWinVersion
  #define MyWinVersion "0.0.0.0"
#endif

[Setup]
AppId={{6F1B7A52-3C4D-4E8A-9B21-7D5E0C3A8F14}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=LoidInfinity
AppPublisherURL=https://github.com/LoidInfinity/Reconnect
AppSupportURL=https://github.com/LoidInfinity/Reconnect/issues
AppUpdatesURL=https://github.com/LoidInfinity/Reconnect/releases
DefaultDirName={localappdata}\Programs\NetworkGuard
UsePreviousAppDir=yes
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\NetworkGuard.exe
SetupIconFile=..\assets\NetworkGuard.ico
OutputDir=..\dist
OutputBaseFilename=NetworkGuard-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
AllowNoIcons=yes
SetupLogging=yes
VersionInfoVersion={#MyWinVersion}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyWinVersion}

[Languages]
Name: "zh"; MessagesFile: "{#SourcePath}\ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; 安装包自身携带的是 PyInstaller 的 onedir 产物，整目录复制
Source: "{#SourceRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\NetworkGuard.exe"
Name: "{group}\{#MyAppName}（命令行）"; Filename: "{app}\start_cli.bat"; WorkingDir: "{app}"; IconFilename: "{app}\NetworkGuardCLI.exe"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\NetworkGuard.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\NetworkGuard.exe"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[Code]
{ 清除下载标记（Mark of the Web）。
  直接在安装程序内用 DeleteFile 删除 :Zone.Identifier 数据流：
  不再通过 powershell -Command 拼接安装路径，避免路径中的引号被当成脚本执行。 }
procedure DeleteZoneIdentifier(const Directory: String);
var
  FindRec: TFindRec;
  ItemPath: String;
begin
  if not FindFirst(AddBackslash(Directory) + '*', FindRec) then
    exit;
  try
    repeat
      if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
      begin
        ItemPath := AddBackslash(Directory) + FindRec.Name;
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
          DeleteZoneIdentifier(ItemPath)
        else
          DeleteFile(ItemPath + ':Zone.Identifier');
      end;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    DeleteZoneIdentifier(ExpandConstant('{app}'));
end;

[UninstallDelete]
; 安装目录内的运行残留（如用户手动生成的日志）一并清理；用户数据目录不在此处，卸载后保留。
Type: filesandordirs; Name: "{app}"
