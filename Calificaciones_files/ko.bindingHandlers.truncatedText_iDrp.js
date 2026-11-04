ko.bindingHandlers.truncatedText={update:function(n,t,e){var a=ko.utils.unwrapObservable(t()),r=ko.utils.unwrapObservable(e().maxTextLength)||20,u=a.length>r?a.substring(0,r)+"...":a;ko.bindingHandlers.text.update(n,function(){return u})}};
//# sourceMappingURL=../../../../../../Scripts/maps/Core/libraries/ko/bindingHandlers/ko.bindingHandlers.truncatedText.js.map
