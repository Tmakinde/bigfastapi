from audioop import reverse
from typing import List
from xmlrpc.client import boolean
from fastapi import APIRouter, Depends, status, HTTPException, File, UploadFile, BackgroundTasks
from bigfastapi.models.organisation_models import Organization
from bigfastapi.models.user_models import User
from bigfastapi.models.customer_models import Customer, OtherInformation, add_customer, put_customer, fetch_customers, add_other_info
from bigfastapi.schemas import customer_schemas, users_schemas
from bigfastapi.models import customer_models
from sqlalchemy.orm import Session
from bigfastapi.db.database import get_db
from uuid import uuid4
from fastapi.responses import JSONResponse
from .auth_api import is_authenticated
from fastapi_pagination import Page, add_pagination, paginate
import csv
import io
from collections import namedtuple
from operator import attrgetter
import pandas as pd

app = APIRouter(tags=["Customers 💁"],)


@app.post("/customers",
          response_model=customer_schemas.CustomerCreateResponse,
          status_code=status.HTTP_201_CREATED
          )
async def create_customer(
    background_tasks: BackgroundTasks,
    customer: customer_schemas.CustomerCreate,
    other_info: List[customer_schemas.OtherInfo] = None, 
    db: Session = Depends(get_db),
    user: users_schemas.User = Depends(is_authenticated)
):
    organization = db.query(Organization).filter(
        Organization.id == customer.organization_id).first()
    if not organization:
        return JSONResponse({"message": "Organization does not exist", "customer": []},
                            status_code=status.HTTP_404_NOT_FOUND)

    existing_customers = await fetch_customers(organization_id=customer.organization_id, db=db)
    for item in existing_customers:
        if customer.unique_id == item.unique_id:
            return JSONResponse({"message": "The given unique_id already exist in the organization", "customer": []},
                                status_code=status.HTTP_406_NOT_ACCEPTABLE)

    customer_instance = await add_customer(customer=customer, organization_id=customer.organization_id, db=db)

    if other_info:
        background_tasks.add_task(add_other_info, other_info, db)

    return {"message": "Customer created succesfully", "customer": customer_instance}


@app.post("/customers/import/{organization_id}",
          response_model=List[customer_schemas.CustomerCreateResponse],
          status_code=status.HTTP_201_CREATED
          )
async def create_bulk_customer(
    organization_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: users_schemas.User = Depends(is_authenticated)
):

    if file.content_type != "text/csv":
        return JSONResponse({"message": "file must be a valid csv", "customer": []},
                            status_code=status.HTTP_406_NOT_ACCEPTABLE)

    organization = db.query(Organization).filter(
        Organization.id == organization_id).first()
    if not organization:
        return JSONResponse({"message": "Organization does not exist", "customer": []},
                            status_code=status.HTTP_404_NOT_FOUND)

    list_customers = await file_to_list_converter(file)
    required_cols = ["first_name", "last_name", "unique_id"]

    df_customers = pd.DataFrame(list_customers)
    sent_columns = df_customers.columns

    for col in required_cols:
        if col not in sent_columns:
            return JSONResponse({"message": f"A required field {col} is missing", "customer": []},
                                status_code=status.HTTP_406_NOT_ACCEPTABLE)

    background_tasks.add_task(unpack_create_customers,
                              df_customers, organization_id, db)

    return JSONResponse({"message": "Creating Customers...", "customer": list_customers},
                        status_code=status.HTTP_201_CREATED)


@app.get('/customers',
         response_model=Page[customer_schemas.Customer],
         status_code=status.HTTP_200_OK
         )
async def get_customers(
    organization_id: str,
    search_value: str = None,
    sorting_key: str = None,
    reverse_sort: bool = False,
    db: Session = Depends(get_db),
    user: users_schemas.User = Depends(is_authenticated)
):

    organization = db.query(Organization).filter(
        Organization.id == organization_id).first()
    if not organization:
        return JSONResponse({"message": "Organization does not exist"}, status_code=status.HTTP_404_NOT_FOUND)

    customers = await fetch_customers(organization_id=organization_id, name=search_value, db=db)

    if not sorting_key or not customers or sorting_key not in customers[0]:
        return paginate(customers)
    customers.sort(key=lambda x: getattr(x, sorting_key), reverse=reverse_sort)
    return paginate(customers)


@app.get('/customers/{customer_id}',
         response_model=customer_schemas.SingleResponse,
         status_code=status.HTTP_200_OK
         )
async def get_customer(
    customer_id: str,
    db: Session = Depends(get_db),
    user: users_schemas.User = Depends(is_authenticated)
):
    customer = db.query(Customer).filter(
        Customer.customer_id == customer_id).first()
    if not customer:
        return JSONResponse({"message": "Customer does not exist"},
                            status_code=status.HTTP_404_NOT_FOUND)

    other_info = db.query(customer_models.OtherInformation).filter(
        OtherInformation.customer_id == customer_id)
    
    setattr(customer, 'other_info', other_info)
    
    return customer_schemas.SingleResponse.from_orm(customer)


@app.put('/customers/{customer_id}',
         response_model=customer_schemas.CustomerCreateResponse,
         status_code=status.HTTP_202_ACCEPTED
         )
async def update_customer(
     background_tasks: BackgroundTasks,
    customer: customer_schemas.CustomerUpdate,
    customer_id: str, 
    other_info: List[customer_schemas.OtherInfo] = None, 
    db: Session = Depends(get_db),
    user: users_schemas.User = Depends(is_authenticated)
):
    customer_instance = db.query(Customer).filter(
        Customer.customer_id == customer_id).first()
    if not customer_instance:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"message": "Customer does not exist"})
    if customer.organization_id:
        organization = db.query(Organization).filter(
            Organization.id == customer.organization_id).first()
        if not organization:
            return JSONResponse({"message": "Organization does not exist"}, status_code=status.HTTP_404_NOT_FOUND)
        customer_instance.organization_id = organization.id

    updated_customer = await put_customer(customer=customer,
                                          customer_instance=customer_instance, db=db)
    
    if other_info:
        background_tasks.add_task(add_other_info, other_info, db)

    return {"message": "Customer updated succesfully", "customer": updated_customer}


@app.delete('/customers/{customer_id}',
            response_model=customer_schemas.ResponseModel,
            status_code=status.HTTP_200_OK
            )
async def soft_delete_customer(
    customer_id: str,
    db: Session = Depends(get_db),
    user: users_schemas.User = Depends(is_authenticated)
):

    customer = db.query(Customer).filter(
        Customer.customer_id == customer_id).first()
    if not customer:
        return JSONResponse({"message": "Customer does not exist"},
                            status_code=status.HTTP_404_NOT_FOUND)
    customer.is_deleted = True
    db.commit()
    db.refresh(customer)
    return JSONResponse({"message": "Customer deleted succesfully"},
                        status_code=status.HTTP_200_OK)


@app.delete('/customers/organization/{organization_id}',
            response_model=customer_schemas.ResponseModel,
            status_code=status.HTTP_200_OK
            )
async def soft_delete_all_customers(
    organization_id: str,
    # user_id: str,
    db: Session = Depends(get_db),
    user: users_schemas.User = Depends(is_authenticated)
):
    # user = db.query(User).filter(User.id == user_id).first()
    # if user.is_superuser != True:
    #     return JSONResponse({"message": "User has no authority to delete all customers"},
    #             status_code=status.HTTP_406_NOT_ACCEPTABLE)

    organization = db.query(Organization).filter(
        Organization.id == organization_id).first()
    if not organization:
        return JSONResponse({"message": "Organization does not exist"},
                            status_code=status.HTTP_404_NOT_FOUND)

    customers = db.query(Customer).filter_by(
        organization_id=organization_id, is_deleted=False)
    for customer in customers:
        customer.is_deleted = True
        db.commit()
        db.refresh(customer)
        print(customer)

    return JSONResponse({"message": "Customers deleted succesfully"},
                        status_code=status.HTTP_200_OK)

add_pagination(app)


#=================================Customer Services==============================#

async def file_to_list_converter(file: UploadFile = File(...)):
    file_bytes = await file.read()
    customer_str = file_bytes.decode()
    reader = csv.DictReader(io.StringIO(customer_str))
    list_customers = []
    for records in reader:
        list_customers.append(records)
    return list_customers


async def unpack_create_customers(df_customers, organization_id: str, db: Session = Depends(get_db)):
    posted_customers = []
    for kwargs in df_customers.to_dict(orient='records'):
        customer = Customer(**kwargs)
        added_customer = await add_customer(customer=customer, organization_id=organization_id, db=db)
        posted_customers.append(added_customer)
    return posted_customers
