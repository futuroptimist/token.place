// This executable excludes registry/MSI discovery and uses only synthetic owners
// and a caller-supplied disposable directory. Safe to run without installation.
#define TOKEN_PLACE_POLICY_TEST
#include "ownership.cpp"
#include <iostream>

int wmain(int argc, wchar_t** argv) {
    try {
        tp::require(argc == 2);
        const tp::wstring a = L"\\\\?\\Volume{test}\\a", b = L"\\\\?\\Volume{test}\\b";
        int cases = 0;
        auto check = [&](bool allowed, bool msi, const tp::wstring& target,
                         std::vector<tp::Owner> owners, bool empty,
                         const tp::wstring& retained = L"") {
            bool actual = true;
            try { tp::authorize(msi, target, owners, empty, retained); }
            catch (const tp::Failure&) { actual = false; }
            if (actual != allowed) throw std::runtime_error("policy case " + std::to_string(cases));
            ++cases;
        };
        for (bool msi : {false, true}) {
            tp::Owner own{msi, true, a, L"own"}, other{!msi, false, b, L"other"};
            check(true, msi, a, {}, true);
            check(false, msi, a, {}, false);
            check(true, msi, a, {own}, false);
            check(false, msi, b, {own}, true);
            check(true, msi, a, {own, other}, false);
            check(false, msi, a, {other}, true);
            check(false, msi, a, {own, own}, false);
            own.file_id = other.file_id = L"same-hard-linked-executable";
            check(false, msi, a, {own, other}, false);
            own.file_id.clear();
            other.file_id.clear();
            for (auto conflict : {a, a + L"\\child", tp::wstring(L"\\\\?\\Volume{test}")}) {
                other.root = conflict;
                check(false, msi, a, {own, other}, false);
                check(false, msi, a, {other}, true, a);
            }
            other.root = b;
            check(true, msi, a, {other}, false, a);
            check(false, msi, b, {other}, false, a);
            other.root = a + L"-separate";
            check(true, msi, a, {own, other}, false);
        }
        auto root = tp::fs::absolute(argv[1]);
        tp::require(!tp::fs::exists(root));
        tp::fs::create_directories(root / L"exists");
        auto original = tp::canonical((root / L"exists").wstring());
        auto different_case = (root / L"EXISTS").wstring();
        tp::require(original == tp::canonical(different_case));
        ++cases;
        tp::verify_requested_upgrade_target(different_case, (root / L"exists").wstring());
        tp::verify_requested_upgrade_target(L"", (root / L"exists").wstring());
        tp::verify_requested_upgrade_target((root / L"fresh").wstring(), L"");
        cases += 3;
        for (auto changed : {root / L"other", root / L"exists" / L"nested", root}) {
            bool rejected = false;
            try { tp::verify_requested_upgrade_target(changed.wstring(), (root / L"exists").wstring()); }
            catch (const tp::Failure&) { rejected = true; }
            tp::require(rejected);
            ++cases;
        }
        tp::require(tp::equal(original, tp::canonical((root / L"exists" / L".").wstring())));
        ++cases;
        auto future = tp::canonical((root / L"exists" / L"new" / L"child").wstring());
        tp::require(tp::contains(original, future));
        ++cases;
        tp::require(tp::empty_target((root / L"exists").wstring()));
        ++cases;
        tp::require(tp::empty_target((root / L"not-created").wstring()));
        ++cases;
        tp::require(!tp::empty_target(root.wstring()));
        ++cases;
        auto report = root / L"ownership.ini";
        auto unicode_path = root / L"\u96ea-\u03bb-\u0416";
        tp::fs::create_directories(unicode_path);
        const tp::wstring unicode = tp::canonical(unicode_path.wstring());
        tp::require(tp::contains(tp::canonical(root.wstring()), unicode));
        tp::require(unicode.find(L"\u96ea-\u03bb-\u0416") != tp::wstring::npos);
        ++cases;
        check(true, false, unicode, {{false, true, unicode, L"unicode-owner"}}, false);
        tp::write_result(report.c_str(), unicode);
        wchar_t result[32768];
        tp::require(GetPrivateProfileStringW(L"ownership", L"target", L"", result, 32768, report.c_str()) == unicode.size());
        tp::require(unicode == result);
        ++cases;
        tp::fs::remove_all(root);
        std::cout << cases << " ownership policy/path cases passed; registry discovery excluded\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
