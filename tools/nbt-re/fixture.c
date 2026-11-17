/* Self-authored static-analysis fixture; never needs to execute on a head unit. */
__attribute__((noinline)) int hu_scale_rpm(int raw)
{
    if (raw < 0)
        return -1;
    return raw * 25;
}

int main(int argc, char **argv)
{
    (void)argv;
    return hu_scale_rpm(argc);
}
