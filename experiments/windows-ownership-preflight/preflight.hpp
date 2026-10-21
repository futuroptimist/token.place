#pragma once
#include <set>
#include <string>
#include <vector>

// Experimental policy only. No Windows API, hive loader, installer entry point,
// process launcher, privilege adjustment, or filesystem access is linked here.
namespace ownership {
enum class Format { nsis, msi, unclassified };
enum class Storage { loaded, offline, unavailable };
enum class Kind { local, roaming, mandatory, temporary, service, unclassified };
enum class ReadState { complete, denied, missing, corrupt, busy, unsupported };
enum class Decision { clear, conflict, incomplete };
struct Profile {
    std::string identity;
    Storage storage;
    Kind kind;
};
struct Inventory {
    bool complete = false;
    std::string revision; // Adapter must detect changes, not merely count profiles.
    std::vector<Profile> profiles;
};
struct Observation {
    ReadState state = ReadState::unsupported;
    // Only authoritative token.place registrations belong here. Adapters must
    // verify product identity and scope; executable-path equality is insufficient.
    std::vector<Format> owners;
};
class Reader {
public:
    virtual ~Reader() = default;
    virtual Inventory inventory() = 0;
    virtual Observation machine() = 0;
    virtual Observation loaded(const Profile&) = 0;
    virtual Observation offline(const Profile&) = 0;
};
struct Result {
    Decision decision = Decision::incomplete;
    unsigned conflicts = 0;
    unsigned unknowns = 0;
};
inline bool known_format(Format format) {
    return format == Format::msi || format == Format::nsis;
}
inline bool known_kind(Kind kind) {
    switch (kind) {
    case Kind::local: case Kind::roaming: case Kind::mandatory:
    case Kind::temporary: case Kind::service: return true;
    default: return false;
    }
}
inline bool same_inventory(const Inventory& a, const Inventory& b) {
    if (!a.complete || !b.complete || a.revision.empty() || a.revision != b.revision
        || a.profiles.size() != b.profiles.size()) return false;
    std::set<std::string> entries;
    for (const auto& p : a.profiles)
        entries.insert(p.identity + ":" + std::to_string(static_cast<int>(p.storage))
                       + ":" + std::to_string(static_cast<int>(p.kind)));
    for (const auto& p : b.profiles)
        if (entries.erase(p.identity + ":" + std::to_string(static_cast<int>(p.storage))
                          + ":" + std::to_string(static_cast<int>(p.kind))) != 1) return false;
    return entries.empty();
}
inline Result inspect(Format requested, Reader& reader) noexcept {
    Result result;
    if (!known_format(requested)) { result.unknowns = 1; return result; }
    const auto consume = [&](const Observation& observation) {
        if (observation.state != ReadState::complete) ++result.unknowns;
        for (const auto owner : observation.owners) {
            if (!known_format(owner)) ++result.unknowns;
            else if (owner != requested) ++result.conflicts;
        }
    };
    const auto consume_safely = [&](const auto& read) {
        try { consume(read()); }
        catch (...) { ++result.unknowns; }
    };
    try {
        Inventory before;
        try { before = reader.inventory(); }
        catch (...) { ++result.unknowns; }
        if (!before.complete || before.revision.empty()) ++result.unknowns;
        consume_safely([&] { return reader.machine(); });
        std::set<std::string> seen;
        for (const auto& profile : before.profiles) {
            if (profile.identity.empty() || !known_kind(profile.kind)) {
                ++result.unknowns;
                continue;
            }
            if (!seen.insert(profile.identity).second) ++result.unknowns;
            // Duplicate inventory entries remain uncertain, but must not hide
            // readable ownership evidence (including alternate loaded/offline state).
            // No blanket exclusions for signed-out, roaming, temporary or service
            // profiles. An adapter must establish evidence or report incomplete.
            switch (profile.storage) {
            case Storage::loaded: consume_safely([&] { return reader.loaded(profile); }); break;
            case Storage::offline: consume_safely([&] { return reader.offline(profile); }); break;
            case Storage::unavailable: default: ++result.unknowns; break;
            }
        }
        if (!same_inventory(before, reader.inventory())) ++result.unknowns;
    } catch (...) {
        ++result.unknowns; // Exceptions must never turn missing evidence into clear.
    }
    result.decision = result.conflicts ? Decision::conflict
        : result.unknowns ? Decision::incomplete : Decision::clear;
    return result;
}
inline const char* message(Decision decision) {
    switch (decision) {
    case Decision::clear: return "Synthetic ownership inspection complete: no competing format found.";
    case Decision::conflict: return "Competing installer found. Uninstall the existing version first.";
    case Decision::incomplete: return "Ownership could not be verified. Existing installations must remain unchanged.";
    }
    return "Ownership could not be verified.";
}
} // namespace ownership
