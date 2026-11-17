#pragma once
int rig_tap_open(const char *name);
long rig_tap_read(int fd,void *frame,unsigned long cap);   /* non-blocking; <=0 when nothing waits */
long rig_tap_write(int fd,const void *frame,unsigned long len);
