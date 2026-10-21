/* Own one native ASR process. A dead Python parent must not leave inference running. */
#include <errno.h>
#include <signal.h>
#include <stdlib.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile sig_atomic_t stopping = 0;
static void stop(int sig) { (void)sig; stopping = 1; }

int main(int argc, char **argv) {
    if (argc < 3 || argv[2][0] != '/') return 64;
    pid_t parent = (pid_t)strtol(argv[1], NULL, 10);
    if (parent <= 1 || getppid() != parent) return 65;
    signal(SIGTERM, stop); signal(SIGINT, stop); signal(SIGHUP, stop);
    pid_t child = fork();
    if (child < 0) return 66;
    if (child == 0) {
        if (setsid() < 0) _exit(67);
        signal(SIGTERM, SIG_DFL); signal(SIGINT, SIG_DFL); signal(SIGHUP, SIG_DFL);
        execv(argv[2], argv + 2);
        _exit(68);
    }
    int status = 0;
    while (!stopping && getppid() == parent) {
        pid_t done = waitpid(child, &status, WNOHANG);
        if (done == child) {
            kill(-child, SIGKILL);
            return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
        }
        if (done < 0 && errno != EINTR) break;
        usleep(100000);
    }
    /* Also target the child directly: cancellation may beat its setsid(). */
    kill(child, SIGTERM); kill(-child, SIGTERM);
    usleep(300000);
    kill(child, SIGKILL); kill(-child, SIGKILL);
    while (waitpid(child, &status, 0) < 0 && errno == EINTR) {}
    return 130;
}
