#ifndef Output
  #error Output, Input and Version are required
#endif
#ifdef LocalTest
  #define AppTitle "gAssets Update Center (Test)"
  #define Folder "gAssets-Test"
  #define ProductKey "Software\gAssets\UpdateCenter.test"
  #define PackageClass "gAssets.SoftwareUpdate.Test"
  #define CenterMutex "Local\gAssets.UpdateCenter.1.test"
#else
  #define AppTitle "gAssets Update Center"
  #define Folder "gAssets"
  #define ProductKey "Software\gAssets\UpdateCenter"
  #define PackageClass "gAssets.SoftwareUpdate"
  #define CenterMutex "Local\gAssets.UpdateCenter.1"
#endif

[Setup]
#ifdef UseAuthenticode
SignTool=gtrpublisher
SignedUninstaller=yes
#endif
AppId=gAssets.UpdateCenter.{#Folder}
AppName={#AppTitle}
AppPublisher=Gruzin
AppPublisherURL=https://github.com/Spalov/gTR
AppVersion=1.0
VersionInfoVersion=1.0.0.0
VersionInfoCompany=Gruzin
VersionInfoCopyright=Copyright © 2026 Gruzin
DefaultDirName={localappdata}\Programs\{#Folder}
DefaultGroupName={#AppTitle}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
DisableProgramGroupPage=yes
WizardStyle=modern
OutputDir={#Output}
OutputBaseFilename=gTR-Beta-{#Version}-Setup-Windows-x64
SetupIconFile={#Input}\gassets.ico
UninstallDisplayIcon={app}\UpdateCenter\gAssets.exe
AppMutex={#CenterMutex}
Compression=lzma2
SolidCompression=no
ChangesAssociations=yes
CloseApplications=no
RestartApplications=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "uk"; MessagesFile: "compiler:Languages\Ukrainian.isl"

[CustomMessages]
ru.DesktopCenter=Создать ярлык gAssets на рабочем столе
uk.DesktopCenter=Створити ярлик gAssets на робочому столі
ru.DesktopGtr=Создать ярлык gTR на рабочем столе
uk.DesktopGtr=Створити ярлик gTR на робочому столі
ru.Title=Установка gTR
uk.Title=Встановлення gTR
ru.Description=Для продолжения проверьте выбранную папку и закройте gTR после завершения текущей операции.
uk.Description=Для продовження перевірте вибрану теку й закрийте gTR після завершення поточної операції.
ru.Failed=gTR не прошёл проверку установки. Откройте gAssets для диагностики или повторите установку.
uk.Failed=gTR не пройшов перевірку встановлення. Відкрийте gAssets для діагностики або повторіть встановлення.
ru.Blocked=Политика Windows не разрешила проверочный запуск gTR. Рабочая версия сохранена.
uk.Blocked=Політика Windows заборонила перевірочний запуск gTR. Робочу версію збережено.
ru.CenterBlocked=Политика Windows заблокировала запуск gAssets. Для установки нужен комплект с доверенной подписью Windows.
uk.CenterBlocked=Політика Windows заблокувала запуск gAssets. Для встановлення потрібен комплект із довіреним підписом Windows.
ru.Framework=Для gAssets требуется установленный .NET Framework 4.8 или новее.
uk.Framework=Для gAssets потрібен встановлений .NET Framework 4.8 або новіший.

[Tasks]
Name: "centerdesktop"; Description: "{cm:DesktopCenter}"; Flags: unchecked
Name: "gtrdesktop"; Description: "{cm:DesktopGtr}"; Flags: unchecked

[Files]
Source: "{#Input}\gAssets.exe"; DestDir: "{app}\UpdateCenter"; Flags: ignoreversion
Source: "{#Input}\gAssets.exe.config"; DestDir: "{app}\UpdateCenter"; Flags: ignoreversion
Source: "{#Input}\gsu.ico"; DestDir: "{app}\UpdateCenter"; Flags: ignoreversion
Source: "{#Input}\TERMS.txt"; DestDir: "{app}\UpdateCenter"; Flags: ignoreversion
Source: "{#Input}\gassets-catalog.json"; DestDir: "{app}\UpdateCenter"; Flags: ignoreversion
Source: "{#Input}\gassets-catalog.sig"; DestDir: "{app}\UpdateCenter"; Flags: ignoreversion
Source: "{#Input}\gTR-Beta-{#Version}-Windows-x64.gsu"; DestDir: "{tmp}"; Flags: deleteafterinstall nocompression

[Icons]
Name: "{autoprograms}\{#AppTitle}"; Filename: "{app}\UpdateCenter\gAssets.exe"
Name: "{autodesktop}\{#AppTitle}"; Filename: "{app}\UpdateCenter\gAssets.exe"; Tasks: centerdesktop

[Registry]
Root: HKCU; Subkey: "{#ProductKey}"; ValueType: string; ValueName: "ProgramRoot"; ValueData: "{app}"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.gsu\OpenWithProgids"; ValueType: string; ValueName: "{#PackageClass}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\{#PackageClass}"; ValueType: string; ValueData: "Gruzin Software Update Package"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\{#PackageClass}\DefaultIcon"; ValueType: string; ValueData: "{app}\UpdateCenter\gsu.ico"
Root: HKCU; Subkey: "Software\Classes\{#PackageClass}\shell\open\command"; ValueType: string; ValueData: """{app}\UpdateCenter\gAssets.exe"" ""%1"""

[Code]
function InitializeSetup(): Boolean;
var
  Release: Cardinal;
begin
  Result := RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full', 'Release', Release) and (Release >= 528040);
  if not Result then
    MsgBox(CustomMessage('Framework'), mbInformation, MB_OK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Status: Integer;
  Args: String;
begin
  if CurStep = ssPostInstall then
  begin
    Args := '--first-install "' + ExpandConstant('{tmp}\gTR-Beta-{#Version}-Windows-x64.gsu') + '" --quiet';
    if WizardIsTaskSelected('gtrdesktop') then
      Args := Args + ' --desktop';
    if not Exec(ExpandConstant('{app}\UpdateCenter\gAssets.exe'), Args, ExpandConstant('{app}'),
      SW_HIDE, ewWaitUntilTerminated, Status) then
    begin
      if (Status = 577) or (Status = 4551) then
        RaiseException(CustomMessage('CenterBlocked'));
      RaiseException(CustomMessage('Failed'));
    end;
    if Status = 43 then
      RaiseException(CustomMessage('Blocked'));
    if Status <> 0 then
      RaiseException(CustomMessage('Failed'));
  end;
end;
