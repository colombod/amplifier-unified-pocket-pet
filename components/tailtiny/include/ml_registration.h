#pragma once
/* Include after cJSON.h and ml_identity.h. Credential omission is an invariant even when
 * a caller still has a bootstrap key saved. Never turn resume into re-enroll. */
static inline bool ml_registration_auth(cJSON *root,const char *key,uint32_t phase,bool permitted) {
    if(phase==ML_ENROLL_ATTEMPTED||phase==ML_ENROLL_REGISTERED)return true;
    if(phase!=ML_ENROLL_PREPARED||!permitted||!key||!key[0])return false;
    cJSON *auth=cJSON_CreateObject();
    if(!auth)return false;
    /* Own the copied key node: AddStringToObject frees it unscrubbed if its name allocation fails. */
    cJSON *key_item=cJSON_CreateString(key);
    if(!key_item){cJSON_Delete(auth);return false;}
    if(!cJSON_AddItemToObject(auth,"AuthKey",key_item)){
        ml_secure_zero(key_item->valuestring,strlen(key_item->valuestring));
        cJSON_Delete(key_item);cJSON_Delete(auth);return false;
    }
    if(!cJSON_AddItemToObject(root,"Auth",auth)){
        ml_secure_zero(key_item->valuestring,strlen(key_item->valuestring));cJSON_Delete(auth);return false;
    }
    return true;
}
static inline bool ml_registration_result(const cJSON *reply) {
    if(!cJSON_IsObject(reply)||!cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(reply,"MachineAuthorized")))return false;
    const char *fields[]={"Error","AuthURL"};
    for(unsigned i=0;i<2;i++) {
        const cJSON *v=cJSON_GetObjectItemCaseSensitive(reply,fields[i]);
        if(v&&(!cJSON_IsString(v)||v->valuestring[0]))return false;
    }
    return true;
}