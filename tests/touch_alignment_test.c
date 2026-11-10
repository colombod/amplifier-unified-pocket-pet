#include <assert.h>
#include "touch_alignment.h"
int main(void) {
 touch_point_t targets[3]={{64,140},{304,140},{184,340}},raw[3];touch_map_t m;
 for(int i=0;i<3;i++)raw[i]=(touch_point_t){targets[i].x+22,targets[i].y-15};
 assert(touch_map_solve(raw,targets,&m));touch_point_t p=touch_map_apply(&m,(touch_point_t){206,209});
 assert(fabsf(p.x-184)<0.1f && fabsf(p.y-224)<0.1f);
 for(int i=0;i<3;i++)raw[i]=(touch_point_t){targets[i].y*0.8f+10,targets[i].x*1.1f+5};
 assert(touch_map_solve(raw,targets,&m));p=touch_map_apply(&m,(touch_point_t){224*0.8f+10,184*1.1f+5});
 assert(fabsf(p.x-184)<0.1f && fabsf(p.y-224)<0.1f);
 raw[0]=raw[1]=raw[2]=(touch_point_t){4,4};assert(!touch_map_solve(raw,targets,&m));
 return 0;
}
