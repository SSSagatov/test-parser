ANNOUNCEMENTS = """query Announcements($filter: TrdBuyFiltersInput, $limit: Int, $after: Int) {
  TrdBuy(filter: $filter, limit: $limit, after: $after) {
    id numberAnno refBuyStatusId startDate endDate repeatStartDate repeatEndDate
    systemId kato RefBuyStatus { id nameRu code }
  }
}"""

LOTS_MIN = """query LotsMin($filter: LotsFiltersInput, $limit: Int, $after: Int) {
  Lots(filter: $filter, limit: $limit, after: $after) {
    id lotNumber trdBuyId plnPointKatoList systemId isDeleted
    Plans { id isQvazi isActive isDeleted
      PlansKato { refKatoCode isActive isDeleted }
    }
  }
}"""

LOTS_FULL = """query LotsFull($filter: LotsFiltersInput, $limit: Int, $after: Int) {
  Lots(filter: $filter, limit: $limit, after: $after) {
    id lotNumber refLotStatusId lastUpdateDate unionLots count amount nameRu nameKz
    descriptionRu descriptionKz customerId customerBin customerNameRu customerNameKz
    trdBuyNumberAnno trdBuyId dumping refTradeMethodsId refBuyTradeMethodsId
    psdSign consultingServices pointList enstruList plnPointKatoList
    singlOrgSign isLightIndustry isConstructionWork disablePersonId isDeleted systemId indexDate
    RefLotsStatus { id nameRu nameKz code }
    RefTradeMethods { id nameRu nameKz }
    RefBuyTradeMethods { id nameRu nameKz }
    Files { id filePath originalName objectId nameRu nameKz indexDate systemId }
    Plans {
      id rootrecordId sysSubjectsId sysOrganizatorId subjectBiin subjectNameRu subjectNameKz
      nameRu nameKz refTradeMethodsId refUnitsCode count price amount refMonthsId
      refPlnPointStatusId plnPointYear refSubjectTypeId refEnstruCode refFinsourceId
      refAbpCode isQvazi dateCreate timestamp refPointTypeId descRu descKz
      extraDescKz extraDescRu sum1 sum2 sum3 supplyDateRu prepayment
      refJustificationId refAmendmentAgreemTypeId refAmendmAgreemJustifId
      contractPrevPointId disablePersonId transferSysSubjectsId transferType
      refBudgetTypeId createdinActId isActive activeActId isDeleted systemId indexDate
      PlansKato {
        id plnPointsId refKatoCode refCountriesCode fullDeliveryPlaceNameRu
        fullDeliveryPlaceNameKz count isActive isDeleted systemId indexDate
        RefKato { nameRu nameKz code level hij fullNameKz fullNameRu parentCode k ef ab cd }
      }
      PlansSpec {
        id plnPointsId refEkrbId ekrbCode ekrbNameRu ekrbNameKz count price
        refFkrbSubprogramId fkrbSubprogramCode fkrbSubprogramNameRu fkrbSubprogramNameKz
        refFkrbId abpCode abpNameRu abpNameKz amount refFkrbProgramId
        fkrbProgramCode fkrbProgramNameRu fkrbProgramNameKz isActive isDeleted systemId indexDate
      }
      RefUnits { code nameRu nameKz }
      RefSubjectType { id nameRu nameKz }
    }
    TrdBuy {
      id numberAnno nameRu nameKz totalSum countLots refTradeMethodsId refSubjectTypeId
      customerBin customerPid customerNameKz customerNameRu orgBin orgPid orgNameKz orgNameRu
      refBuyStatusId startDate repeatStartDate repeatEndDate endDate publishDate
      itogiDatePublic refTypeTradeId disablePersonId discusStartDate discusEndDate
      idSupplier biinSupplier parentId singlOrgSign isLightIndustry isConstructionWork
      refSpecPurchaseTypeId lastUpdateDate finYear kato systemId indexDate
      RefBuyStatus { id nameRu nameKz code }
      Files { id filePath originalName objectId nameRu nameKz indexDate systemId }
      Cancel { id numberDecision dateDecision nameAuthority dateCreate trdBuyId
               actTypeNameRu actTypeNameKz typeActionsNameRu typeActionsNameKz
               typeActionsCode systemId indexDate }
      Pause { id status dateCreate datePause decideNumber decideDate decideDocKz
              decideDocRu statusNameRu statusNameKz solutionNameRu solutionNameKz
              systemId indexDate }
      Organizer { pid bin nameRu nameKz qvazi customer organizer markNationalCompany }
    }
  }
}"""
