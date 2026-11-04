export function createNativeQueuedUnsupportedContract(options: {tenant:string;account_jid:string;scope:{business_id:number;branch_id:number};sha256(input:string):string}): {
  check(input:any):any;own(input:any):any;canonical(input:any):string;metadataHash(input:any):string;receipt(input:any,response:any):any;checkReceipt(input:any,request:any):any;
};
