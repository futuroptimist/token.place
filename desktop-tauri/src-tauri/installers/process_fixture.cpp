// Disposable hosted-CI process. No application behavior, registry or data access.
#include <cstdlib>
#include <iostream>
#include <string>
int main() {
    char* hosted = nullptr; size_t length = 0;
    if (_dupenv_s(&hosted, &length, "RUNNER_ENVIRONMENT") || !hosted) return 2;
    bool allowed = std::string(hosted) == "github-hosted";
    free(hosted);
    if (!allowed) return 2;
    std::cout << "fixture-ready" << std::endl;
    std::cin.get(); // Controller closes only its own pipe to request normal exit.
    return 0;
}
