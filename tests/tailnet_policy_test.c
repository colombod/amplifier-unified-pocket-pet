#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "ml_policy.h"
int main(void) {
    assert(ml_policy_name("node.example.ts.net","node.example.ts.net."));
    assert(!ml_policy_name("node.example.ts.net","node.example.ts.net.evil"));
    assert(!ml_policy_name("node.example.ts.net","node"));
    ml_policy_t p={0};uint8_t key[32]={1},other[32]={2};
    assert(!ml_policy_peer(&p,"node.example.ts.net",0x64400102,key));
    strcpy(p.host,"node.example.ts.net");p.port=8941;
    assert(!ml_policy_peer(&p,"other.example.ts.net",0x64400102,key));
    assert(ml_policy_peer(&p,p.host,0x64400102,key));
    assert(!ml_policy_peer(&p,p.host,0x64400103,other) && p.conflict);
    p.conflict=false;
    uint8_t tcp[40]={0x45,0,0,40,0,0,0x40,0,64,6};
    tcp[12]=100;tcp[13]=64;tcp[14]=1;tcp[15]=1;
    tcp[16]=100;tcp[17]=64;tcp[18]=1;tcp[19]=2;
    tcp[20]=0xc0;tcp[21]=0x01;tcp[22]=8941>>8;tcp[23]=8941&255;tcp[32]=0x50;tcp[33]=2;
    /* Same plaintext fence is installed before encryption and after decryption,
       so both direct UDP and DERP WG transport share this policy. */
    assert(ml_policy_packet(&p,tcp,40,false,0x64400101));
    assert(p.local_port==0xc001);
    tcp[23]=80;assert(!ml_policy_packet(&p,tcp,40,false,0x64400101));tcp[23]=8941&255;
    tcp[19]=3;assert(!ml_policy_packet(&p,tcp,40,false,0x64400101));tcp[19]=2;
    tcp[6]=0x20;assert(!ml_policy_packet(&p,tcp,40,false,0x64400101));tcp[6]=0x40;
    for(int i=0;i<4;i++){uint8_t v=tcp[12+i];tcp[12+i]=tcp[16+i];tcp[16+i]=v;}
    tcp[20]=8941>>8;tcp[21]=8941&255;tcp[22]=0xc0;tcp[23]=1;tcp[33]=0x12;
    assert(ml_policy_packet(&p,tcp,40,true,0x64400101));
    tcp[33]=2;assert(!ml_policy_packet(&p,tcp,40,true,0x64400101));
    p.local_port=0;assert(!ml_policy_packet(&p,tcp,40,true,0x64400101));
    puts("PASS production fail-closed peer identity and bidirectional TCP tuple fence");
}