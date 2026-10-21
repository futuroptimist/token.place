#include "preflight.hpp"
#include <iostream>
#include <map>
#include <stdexcept>
#include <utility>

using namespace ownership;
namespace {
// Each fixture owns its synthetic users and data. Nothing refers to host SIDs,
// account databases, registry roots, profile paths, or installed applications.
struct Fixture final : Reader {
    Inventory first{true, "fixture-generation-1", {{"alice", Storage::loaded, Kind::local},
                                                 {"bob", Storage::offline, Kind::local}}};
    Inventory last = first;
    Observation system{ReadState::complete, {}};
    std::map<std::string, Observation> online{{"alice", {ReadState::complete, {}}}};
    std::map<std::string, Observation> disk{{"bob", {ReadState::complete, {}}}};
    std::map<std::string, std::string> preserved{
        {"alice/app", "old binary"}, {"alice/settings", "custom settings"},
        {"bob/app", "other user binary"}, {"bob/model", "external GGUF fixture"}};
    unsigned inventories = 0;
    unsigned offline_reads = 0;
    unsigned loaded_reads = 0;
    bool throws = false;
    Inventory inventory() override { return inventories++ == 0 ? first : last; }
    Observation machine() override {
        if (throws) throw std::runtime_error("synthetic read error");
        return system;
    }
    Observation loaded(const Profile& p) override { ++loaded_reads; return online.at(p.identity); }
    Observation offline(const Profile& p) override { ++offline_reads; return disk.at(p.identity); }
};
unsigned cases = 0;
void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
Result check(Fixture fixture, Format requested, Decision expected) {
    const auto before = fixture.preserved;
    const auto online = fixture.online;
    const auto disk = fixture.disk;
    const auto result = inspect(requested, fixture);
    require(result.decision == expected, "unexpected ownership decision");
    require(fixture.preserved == before, "fixture app/settings/models changed");
    const auto unchanged = [](const auto& a, const auto& b) {
        if (a.size() != b.size()) return false;
        for (const auto& entry : a) {
            const auto found = b.find(entry.first);
            if (found == b.end() || found->second.state != entry.second.state
                || found->second.owners != entry.second.owners) return false;
        }
        return true;
    };
    require(unchanged(fixture.online, online) && unchanged(fixture.disk, disk), "ownership fixture changed");
    ++cases;
    return result;
}
void tests() {
    check({}, Format::msi, Decision::clear); // Signed-out is not automatically unknown.
    check({}, Format::nsis, Decision::clear);
    for (const auto format : {Format::msi, Format::nsis}) {
        Fixture f;
        if (format == Format::msi) f.system.owners = {format};
        else f.online.at("alice").owners = {format};
        check(f, format, Decision::clear); // Same-format upgrade, no blanket bypass.
        f.disk.at("bob").owners = {format == Format::msi ? Format::nsis : Format::msi};
        check(f, format, Decision::conflict);
    }
    for (const auto kind : {Kind::local, Kind::roaming, Kind::mandatory, Kind::temporary, Kind::service}) {
        Fixture f;
        f.first.profiles[1].kind = kind;
        f.last = f.first;
        f.disk.at("bob").owners = {Format::nsis};
        check(f, Format::msi, Decision::conflict);
        f.disk.at("bob").owners.clear();
        check(f, Format::msi, Decision::clear);
    }
    for (const auto state : {ReadState::denied, ReadState::missing, ReadState::corrupt,
                             ReadState::busy, ReadState::unsupported}) {
        Fixture f;
        f.disk.at("bob").state = state;
        check(f, Format::msi, Decision::incomplete);
        f.online.at("alice").owners = {Format::nsis};
        const auto result = check(f, Format::msi, Decision::conflict);
        require(result.unknowns > 0 && result.conflicts > 0, "lost incomplete/conflict evidence");
    }
    Fixture f;
    f.system.owners = {Format::msi};
    check(f, Format::nsis, Decision::conflict);
    f.system.state = ReadState::denied;
    f.system.owners.clear();
    check(f, Format::nsis, Decision::incomplete);
    f = Fixture{}; f.last.revision = "profile-login";
    check(f, Format::msi, Decision::incomplete);
    f = Fixture{}; f.last.profiles[1].storage = Storage::loaded;
    check(f, Format::msi, Decision::incomplete);
    f = Fixture{}; f.first.complete = false;
    check(f, Format::msi, Decision::incomplete);
    f = Fixture{}; f.first.profiles.push_back(f.first.profiles.front()); f.last = f.first;
    check(f, Format::msi, Decision::incomplete);
    f = Fixture{}; f.first.profiles[1].identity.clear(); f.last = f.first;
    check(f, Format::msi, Decision::incomplete);
    f = Fixture{}; f.first.profiles[1].storage = Storage::unavailable; f.last = f.first;
    check(f, Format::msi, Decision::incomplete);
    f = Fixture{}; f.first.profiles[1].kind = Kind::unclassified; f.last = f.first;
    check(f, Format::msi, Decision::incomplete);
    f = Fixture{}; f.throws = true;
    check(f, Format::msi, Decision::incomplete);
    f = Fixture{}; f.disk.clear(); // Missing adapter record throws, never becomes absence.
    check(f, Format::msi, Decision::incomplete);
    f = Fixture{}; f.system.owners = {Format::unclassified};
    check(f, Format::msi, Decision::incomplete);
    check({}, Format::unclassified, Decision::incomplete);
    f = Fixture{};
    require(inspect(Format::msi, f).decision == Decision::clear, "normal inspection failed");
    require(f.loaded_reads == 1 && f.offline_reads == 1, "wrong profile reader selected");
    require(std::string(message(Decision::incomplete)).find("Uninstall") == std::string::npos,
            "unknown evidence falsely claims a conflict");
}
}
int main() {
    try {
        tests();
        std::cout << "PASS: " << cases << " synthetic multi-user cases; no native registry/hive adapters linked.\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
