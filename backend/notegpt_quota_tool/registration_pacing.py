"""Conservative pacing that backs off instead of retrying rate-limited traffic."""
class RegistrationPacing:
    def __init__(self,base=60,initial=180,maximum=1800):
        self.base=max(5,int(base));self.initial=max(self.base,int(initial))
        self.maximum=max(self.initial,int(maximum));self.delay=self.base;self.clean=0

    def after_group(self,results):
        limited=any('too many requests' in str(r.error).lower() or '164010' in str(r.error) or '429' in str(r.error) for r in results)
        if limited:
            self.clean=0;self.delay=min(self.maximum,max(self.initial,self.delay*2))
            return self.delay,'rate_limit'
        if any(not r.success for r in results):
            self.clean=0
            return self.delay,'group_errors'
        self.clean+=1
        if self.clean>=2:
            self.delay=max(self.base,self.delay//2);self.clean=0
        return self.delay,'normal'
