// Read-only ownership preflight. No profile enumeration, hive loading, elevation,
// process termination, registry writes, or application-data access.
#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <msi.h>
#include <msiquery.h>
#include <algorithm>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <vector>

namespace tp {
using std::wstring;
namespace fs = std::filesystem;
constexpr auto message = L"TOKEN_PLACE_COMPETING_INSTALLER: token.place desktop ownership could not be verified. Uninstall the existing version first, or choose an empty, separate installation folder. Your existing app, settings and models have not been changed.";
struct Failure : std::runtime_error { Failure() : runtime_error("ownership not verified") {} };
void require(bool ok) { if (!ok) throw Failure(); }
bool equal(const wstring& a, const wstring& b) {
    return CompareStringOrdinal(a.c_str(), -1, b.c_str(), -1, TRUE) == CSTR_EQUAL;
}
wstring unquote(wstring value) {
    if (value.size() >= 2 && value.front() == L'"' && value.back() == L'"')
        value = value.substr(1, value.size() - 2);
    require(value.find(L'"') == wstring::npos);
    return value;
}
// Resolve the nearest existing ancestor through a handle, including junctions,
// symlinks, short names and mapped drive aliases. Never create the destination.
wstring canonical(const wstring& input) {
    fs::path path(unquote(input));
    require(path.is_absolute() && path.has_root_name());
    path = path.lexically_normal();
    std::vector<fs::path> missing;
    for (;;) {
        HANDLE handle = CreateFileW(path.c_str(), 0,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
            OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, nullptr);
        if (handle != INVALID_HANDLE_VALUE) {
            wchar_t buffer[32768];
            DWORD n = GetFinalPathNameByHandleW(handle, buffer, 32768, VOLUME_NAME_GUID);
            CloseHandle(handle);
            require(n && n < 32768);
            fs::path resolved(wstring(buffer, n));
            for (auto it = missing.rbegin(); it != missing.rend(); ++it) resolved /= *it;
            auto result = resolved.wstring();
            while (result.size() > 1 && result.back() == L'\\') result.pop_back();
            return result;
        }
        DWORD error = GetLastError();
        require(error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND);
        require(path.has_filename() && path.parent_path() != path);
        missing.push_back(path.filename());
        path = path.parent_path();
    }
}
bool contains(const wstring& parent, const wstring& child) {
    return equal(parent, child) || (child.size() > parent.size() &&
        child[parent.size()] == L'\\' && equal(parent, child.substr(0, parent.size())));
}
bool overlaps(const wstring& a, const wstring& b) { return contains(a, b) || contains(b, a); }
bool empty_target(const wstring& path) {
    std::error_code error;
    bool exists = fs::exists(path, error);
    require(!error);
    if (!exists) return true;
    bool empty = fs::is_directory(path, error) && fs::is_empty(path, error);
    require(!error);
    return empty;
}
void verify_owned_tree(const wstring& root) {
    // An owned top-level directory does not authorize writes through an embedded
    // junction/symlink into a different installation (or arbitrary user data).
    for (const auto& entry : fs::recursive_directory_iterator(root)) {
        DWORD attributes = GetFileAttributesW(entry.path().c_str());
        require(attributes != INVALID_FILE_ATTRIBUTES && !(attributes & FILE_ATTRIBUTE_REPARSE_POINT));
    }
}
struct Owner { bool msi; bool current_user; wstring root; wstring identity; wstring file_id = L""; };
// Pure policy: "upgrade" is established by a verified owner, never /UPDATE or
// Installed. A retained owner is used only within NSIS after its own uninstaller.
bool authorize(bool msi, const wstring& target, const std::vector<Owner>& owners,
               bool empty, const wstring& retained = L"") {
    const Owner* own = nullptr;
    bool competing = false;
    for (size_t i = 0; i < owners.size(); ++i)
        for (size_t j = i + 1; j < owners.size(); ++j)
            require(owners[i].file_id.empty() || owners[i].file_id != owners[j].file_id);
    for (const auto& owner : owners) {
        if (owner.msi == msi && (msi || owner.current_user)) {
            require(!own);
            own = &owner;
        } else {
            competing = true;
            require(!overlaps(target, owner.root));
        }
    }
    if (own) { require(equal(target, own->root)); return true; }
    if (!retained.empty()) { require(equal(target, retained)); return true; }
    require(!competing && empty);
    return false;
}

#ifndef TOKEN_PLACE_POLICY_TEST
struct Key {
    HKEY value = nullptr;
    ~Key() { if (value) RegCloseKey(value); }
};
bool open(HKEY hive, const wstring& name, REGSAM view, Key& key) {
    auto result = RegOpenKeyExW(hive, name.c_str(), 0, KEY_READ | view, &key.value);
    if (result == ERROR_FILE_NOT_FOUND) return false;
    require(result == ERROR_SUCCESS);
    return true;
}
wstring string(HKEY key, const wchar_t* name) {
    DWORD type = 0, size = 0;
    auto result = RegQueryValueExW(key, name, nullptr, &type, nullptr, &size);
    if (result == ERROR_FILE_NOT_FOUND) return L"";
    require(result == ERROR_SUCCESS && type == REG_SZ && size >= sizeof(wchar_t) && size < 65536);
    std::vector<wchar_t> buffer(size / sizeof(wchar_t) + 1, 0);
    require(RegQueryValueExW(key, name, nullptr, &type, reinterpret_cast<BYTE*>(buffer.data()), &size) == ERROR_SUCCESS);
    require(type == REG_SZ && size >= sizeof(wchar_t) && size % sizeof(wchar_t) == 0);
    require(buffer[size / sizeof(wchar_t) - 1] == 0 && wcslen(buffer.data()) + 1 == size / sizeof(wchar_t));
    return buffer.data();
}
wstring product_info(const wstring& product, const wchar_t* property) {
    wchar_t buffer[32768]; DWORD size = 32768;
    require(MsiGetProductInfoW(product.c_str(), property, buffer, &size) == ERROR_SUCCESS);
    require(size != 0);
    return buffer;
}
std::vector<Owner> inventory(const wstring& name, const wstring& publisher,
                             const wstring& binary) {
    std::vector<Owner> owners;
    SYSTEM_INFO system{};
    GetNativeSystemInfo(&system);
    std::vector<REGSAM> views{KEY_WOW64_32KEY};
    if (system.wProcessorArchitecture != PROCESSOR_ARCHITECTURE_INTEL) views.push_back(KEY_WOW64_64KEY);
    for (auto hive : {HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE}) {
        for (REGSAM view : views) {
            // HKCU\Software (except Classes) is shared between registry views.
            // Do not count the very same authority twice. HKLM's two uninstall
            // views are distinct and must retain duplicate-authority detection.
            if (hive == HKEY_CURRENT_USER && view == KEY_WOW64_64KEY) continue;
            Key uninstall;
            if (!open(hive, L"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall", view, uninstall)) continue;
            for (DWORD index = 0;; ++index) {
                wchar_t child[256]; DWORD length = 256;
                auto result = RegEnumKeyExW(uninstall.value, index, child, &length, nullptr, nullptr, nullptr, nullptr);
                if (result == ERROR_NO_MORE_ITEMS) break;
                require(result == ERROR_SUCCESS);
                Key key; require(open(uninstall.value, child, view, key));
                auto display = string(key.value, L"DisplayName");
                // A stale named NSIS key is still authority evidence, not absence.
                if (!equal(display, name) && !equal(child, name)) continue;
                require(equal(display, name) && equal(string(key.value, L"Publisher"), publisher));
                DWORD type = 0, flag = 0, size = sizeof(flag);
                result = RegQueryValueExW(key.value, L"WindowsInstaller", nullptr, &type, reinterpret_cast<BYTE*>(&flag), &size);
                bool msi = result == ERROR_SUCCESS && type == REG_DWORD && size == sizeof(flag) && flag == 1;
                require(msi || result == ERROR_FILE_NOT_FOUND);
                auto root = unquote(string(key.value, L"InstallLocation"));
                require(!root.empty() && fs::is_regular_file(fs::path(root) / binary));
                require(contains(canonical(root), canonical((fs::path(root) / binary).wstring())));
                if (msi) {
                    require(equal(product_info(child, INSTALLPROPERTY_INSTALLEDPRODUCTNAME), name));
                    require(equal(product_info(child, INSTALLPROPERTY_PUBLISHER), publisher));
                    require(equal(canonical(product_info(child, INSTALLPROPERTY_INSTALLLOCATION)), canonical(root)));
                } else {
                    require(equal(child, name));
                    auto command = string(key.value, L"UninstallString");
                    auto executable = (fs::path(root) / L"uninstall.exe").wstring();
                    require(equal(command, L"\"" + executable + L"\"") && fs::is_regular_file(executable));
                    Key remembered;
                    require(open(hive, L"Software\\" + publisher + L"\\" + name, view, remembered));
                    require(equal(canonical(string(remembered.value, L"")), canonical(root)));
                }
                Owner owner{msi, hive == HKEY_CURRENT_USER, canonical(root), child};
                HANDLE file = CreateFileW((fs::path(root) / binary).c_str(), 0,
                    FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING, 0, nullptr);
                require(file != INVALID_HANDLE_VALUE);
                BY_HANDLE_FILE_INFORMATION info{};
                bool queried = GetFileInformationByHandle(file, &info) != 0;
                CloseHandle(file);
                require(queried);
                owner.file_id = std::to_wstring(info.dwVolumeSerialNumber) + L":" +
                    std::to_wstring(info.nFileIndexHigh) + L":" + std::to_wstring(info.nFileIndexLow);
                owners.push_back(owner);
            }
        }
    }
    return owners;
}
#endif
} // namespace tp

#if defined(TOKEN_PLACE_MSI)
namespace {
std::wstring property(MSIHANDLE session, const wchar_t* name) {
    wchar_t buffer[32768]; DWORD size = 32768;
    tp::require(MsiGetPropertyW(session, name, buffer, &size) == ERROR_SUCCESS);
    return buffer;
}
std::wstring target_path(MSIHANDLE session) {
    wchar_t buffer[32768]; DWORD size = 32768;
    tp::require(MsiGetTargetPathW(session, L"INSTALLDIR", buffer, &size) == ERROR_SUCCESS);
    return buffer;
}
UINT refusal(MSIHANDLE session) {
    MSIHANDLE record = MsiCreateRecord(1);
    MsiRecordSetStringW(record, 0, tp::message);
    MsiProcessMessage(session, INSTALLMESSAGE_ERROR, record);
    MsiCloseHandle(record);
    return ERROR_INSTALL_FAILURE;
}
tp::Owner related(MSIHANDLE session) {
    wchar_t product[39]; tp::Owner own{true, false, L"", L""};
    auto upgrade = property(session, L"UpgradeCode");
    for (DWORD index = 0;; ++index) {
        auto result = MsiEnumRelatedProductsW(upgrade.c_str(), 0, index, product);
        if (result == ERROR_NO_MORE_ITEMS) break;
        tp::require(result == ERROR_SUCCESS && own.identity.empty());
        tp::require(tp::equal(tp::product_info(product, INSTALLPROPERTY_INSTALLEDPRODUCTNAME), property(session, L"ProductName")));
        own.root = tp::product_info(product, INSTALLPROPERTY_INSTALLLOCATION);
        own.identity = product;
    }
    return own;
}
}
extern "C" __declspec(dllexport) UINT __stdcall TokenPlaceResolve(MSIHANDLE session) {
    try {
        auto own = related(session);
        if (!own.root.empty() && property(session, L"INSTALLDIR").empty())
            tp::require(MsiSetPropertyW(session, L"INSTALLDIR", own.root.c_str()) == ERROR_SUCCESS);
        return ERROR_SUCCESS;
    } catch (...) { return refusal(session); }
}
extern "C" __declspec(dllexport) UINT __stdcall TokenPlaceVerify(MSIHANDLE session) {
    try {
        auto target = target_path(session);
        auto owners = tp::inventory(property(session, L"ProductName"), property(session, L"Manufacturer"), property(session, L"TOKEN_PLACE_BINARY"));
        auto own = related(session);
        for (const auto& owner : owners) if (owner.msi)
            tp::require(!own.root.empty() && tp::equal(owner.root, tp::canonical(own.root)) && tp::equal(owner.identity, own.identity));
        bool upgrade = tp::authorize(true, tp::canonical(target), owners, tp::empty_target(target));
        tp::require(upgrade == !own.root.empty());
        if (upgrade) tp::verify_owned_tree(target);
        return ERROR_SUCCESS;
    } catch (...) { return refusal(session); }
}
#elif !defined(TOKEN_PLACE_POLICY_TEST)
int wmain(int argc, wchar_t** argv) {
    try {
        tp::require(argc == 7);
        auto owners = tp::inventory(argv[2], argv[3], argv[4]);
        auto target = tp::canonical(argv[1]);
        bool upgrade = tp::authorize(false, target, owners,
            tp::empty_target(argv[1]), argv[5]);
        if (upgrade && tp::fs::exists(argv[1])) tp::verify_owned_tree(argv[1]);
        // Only the installer's private temporary directory receives this result.
        tp::require(WritePrivateProfileStringW(L"ownership", L"target", target.c_str(), argv[6]));
        return upgrade ? 10 : 0;
    } catch (...) { return 1638; }
}
#endif
