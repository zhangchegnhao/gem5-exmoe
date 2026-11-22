/* Small deterministic INT32 GEMM for environment checks, not LLM benchmarking. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define MAX_N 256
static int32_t a[MAX_N][MAX_N];
static int32_t b[MAX_N][MAX_N];
static int32_t c[MAX_N][MAX_N];

int main(int argc, char **argv)
{
    int n = argc > 1 ? atoi(argv[1]) : 32;
    if (n < 1 || n > MAX_N) {
        fprintf(stderr, "Matrix dimension must be between 1 and %d\n", MAX_N);
        return 1;
    }
    for (int i = 0; i < n; ++i)
        for (int j = 0; j < n; ++j) {
            a[i][j] = (i + j) % 7 - 3;
            b[i][j] = (i * 3 + j) % 5 - 2;
        }

    for (int i = 0; i < n; ++i)
        for (int j = 0; j < n; ++j)
            for (int k = 0; k < n; ++k)
                c[i][j] += a[i][k] * b[k][j];

    int64_t checksum = 0;
    for (int i = 0; i < n; ++i)
        for (int j = 0; j < n; ++j)
            checksum += c[i][j];
    printf("matmul n=%d checksum=%lld\n", n, (long long)checksum);
    return 0;
}
