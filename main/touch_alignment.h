#pragma once
#include <math.h>
#include <stdbool.h>
typedef struct { float x,y; } touch_point_t;
typedef struct { float a,b,c,d,e,f; } touch_map_t;
static inline bool touch_map_solve(const touch_point_t raw[3],const touch_point_t target[3],touch_map_t *out) {
 float x=raw[0].x,y=raw[0].y,dx1=raw[1].x-x,dy1=raw[1].y-y,dx2=raw[2].x-x,dy2=raw[2].y-y;
 float det=dx1*dy2-dx2*dy1;if(fabsf(det)<1000)return false;
 float tx1=target[1].x-target[0].x,tx2=target[2].x-target[0].x;
 float ty1=target[1].y-target[0].y,ty2=target[2].y-target[0].y;
 touch_map_t m={.a=(tx1*dy2-tx2*dy1)/det,.b=(dx1*tx2-dx2*tx1)/det,
 .d=(ty1*dy2-ty2*dy1)/det,.e=(dx1*ty2-dx2*ty1)/det};
 m.c=target[0].x-m.a*x-m.b*y;m.f=target[0].y-m.d*x-m.e*y;
 if(!isfinite(m.a)||!isfinite(m.b)||!isfinite(m.c)||!isfinite(m.d)||!isfinite(m.e)||!isfinite(m.f) ||
 fabsf(m.a)>5||fabsf(m.b)>5||fabsf(m.d)>5||fabsf(m.e)>5||fabsf(m.c)>500||fabsf(m.f)>500)return false;
 *out=m;return true;
}
static inline touch_point_t touch_map_apply(const touch_map_t *m,touch_point_t raw) {
 return (touch_point_t){m->a*raw.x+m->b*raw.y+m->c,m->d*raw.x+m->e*raw.y+m->f};
}
