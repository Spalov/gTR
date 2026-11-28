#define UNICODE
#define NOMINMAX
#include <windows.h>

#include <filesystem>
#include <string>
#include <vector>

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR arguments, int) {
    std::vector<wchar_t> module(32768);
    const DWORD length = GetModuleFileNameW(nullptr, module.data(), static_cast<DWORD>(module.size()));
    if (length == 0 || length >= module.size())
        return 2;
    const auto root = std::filesystem::path(module.data()).parent_path();
    const auto shell = root / L"runtime" / L"Lib" / L"site-packages" / L"PySide6" / L"gTR-shell.exe";
    std::wstring command = L"\"" + shell.wstring() + L"\" " + arguments;
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process{};
    if (!CreateProcessW(shell.c_str(), command.data(), nullptr, nullptr, FALSE, 0,
                        nullptr, root.c_str(), &startup, &process)) {
        const DWORD code = GetLastError();
        const bool blocked = code == ERROR_INVALID_IMAGE_HASH || code == ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION;
        if (std::wstring(arguments) != L"--health-check") {
            const auto message = blocked ? L"Политика запуска Windows не разрешает запустить эту сборку gTR."
                                         : L"Проверьте, что комплект gTR полностью распакован.";
            MessageBoxW(nullptr, message, L"gTR", MB_OK | MB_ICONERROR);
        }
        return blocked ? 43 : 2;
    }
    CloseHandle(process.hThread);
    WaitForSingleObject(process.hProcess, INFINITE);
    DWORD status = 2;
    GetExitCodeProcess(process.hProcess, &status);
    CloseHandle(process.hProcess);
    return static_cast<int>(status);
}
