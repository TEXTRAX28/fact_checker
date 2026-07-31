#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv) {
    char command[32768];
    if (argc == 3 && strcmp(argv[1], "-Z1") == 0) {
        snprintf(command, sizeof(command), "tar -tf \"%s\"", argv[2]);
        return system(command);
    }
    if (argc == 4 && strcmp(argv[1], "-p") == 0) {
        snprintf(command, sizeof(command), "tar -xOf \"%s\" \"%s\"", argv[2], argv[3]);
        return system(command);
    }
    fprintf(stderr, "Unsupported unzip arguments\n");
    return 2;
}
