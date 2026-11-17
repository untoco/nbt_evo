#define _POSIX_C_SOURCE 200112L
#include <stdio.h>
#include <sys/utsname.h>
#include <time.h>

int main(void)
{
    struct utsname system;
    puts("Clubsport: hello from a standalone QNX program.");
    if (uname(&system) != 0) {
        perror("uname");
        return 1;
    }
    printf("System: %s %s; machine: %s\n",
           system.sysname, system.release, system.machine);
    printf("Pointer bytes: %u; time_t bytes: %u\n",
           (unsigned)sizeof(void *), (unsigned)sizeof(time_t));
    return 0;
}
